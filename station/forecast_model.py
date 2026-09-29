"""Station-side trend reference for probe-adjacent air, not bulk-grain physics.

Snapshots are reduced to the latest valid sample in each fixed 30-minute
bucket. Station has durable timestamps and can verify bucket continuity; the
embedded S3 model uses its own bounded RAM history and is intentionally not
changed by this module.
"""
import math

HORIZONS_HOURS = (1, 3, 6)
SAMPLE_PERIOD_SECONDS = 1800
MAX_SAMPLES = 100
MIN_SAMPLES = 6
DEFAULT_MAX_AGE_SECONDS = 2700
HISTORY_MULTIPLIER = 2
MIN_TEMP_C = -40.0
MAX_TEMP_C = 125.0


def saturation_vapor_pressure_hpa(temp_c):
    return 6.112 * math.exp((17.62 * temp_c) / (243.12 + temp_c))


def _median(values):
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _theil_sen_per_hour(points):
    slopes = []
    for i, left in enumerate(points):
        for right in points[i + 1:]:
            elapsed_h = (right[0] - left[0]) / 3600.0
            if elapsed_h > 0.0:
                slopes.append((right[1] - left[1]) / elapsed_h)
    return _median(slopes) if slopes else 0.0


def _bucket_center(ts):
    return (math.floor(ts / SAMPLE_PERIOD_SECONDS) * SAMPLE_PERIOD_SECONDS +
            SAMPLE_PERIOD_SECONDS / 2.0)


def _empty(status, count=0, span=0, latest_ts=None):
    return {
        "status": status,
        "sample_count": count,
        "span_seconds": span,
        "latest_ts": latest_ts,
        "temperature_slope_c_per_h": None,
        "vapor_pressure_slope_hpa_per_h": None,
        "forecast": [],
        "valid_horizons": [],
    }


def _sample_history(samples):
    """Keep the latest valid sample from each fixed 30-minute time bucket."""
    by_bucket = {}
    for sample in samples:
        try:
            ts = float(sample["ts"])
            temp = float(sample["temp"])
            rh = float(sample["rh"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (not math.isfinite(ts) or not math.isfinite(temp) or
                not math.isfinite(rh) or temp < MIN_TEMP_C or
                temp > MAX_TEMP_C or rh < 0.0 or rh > 100.0):
            continue
        bucket = math.floor(ts / SAMPLE_PERIOD_SECONDS)
        previous = by_bucket.get(bucket)
        if previous is None or ts > previous[0]:
            by_bucket[bucket] = (ts, temp, rh)
    return sorted(by_bucket.values())[-MAX_SAMPLES:]


def _horizon_coverage(points):
    """Require a complete run of half-hour buckets for each forecast horizon."""
    if not points:
        return []
    last_ts = points[-1][0]
    last_bucket = math.floor(last_ts / SAMPLE_PERIOD_SECONDS)
    coverage = []
    for i, horizon in enumerate(HORIZONS_HOURS):
        required_span = max(
            horizon * HISTORY_MULTIPLIER * 3600,
            (MIN_SAMPLES - 1) * SAMPLE_PERIOD_SECONDS)
        required_intervals = int(math.ceil(
            required_span / float(SAMPLE_PERIOD_SECONDS)))
        first_bucket = last_bucket - required_intervals
        window = [point for point in points
                  if first_bucket <= math.floor(
                      point[0] / SAMPLE_PERIOD_SECONDS) <= last_bucket]
        present = {math.floor(point[0] / SAMPLE_PERIOD_SECONDS)
                   for point in window}
        complete = all(bucket in present for bucket in
                       range(first_bucket, last_bucket + 1))
        history_span = int(window[-1][0] - window[0][0]) if window else 0
        coverage.append({
            "hour": horizon,
            "required_span_seconds": required_span,
            "sample_count": len(window),
            "required_bucket_count": required_intervals + 1,
            "bucket_span_seconds": required_intervals * SAMPLE_PERIOD_SECONDS,
            "history_span_seconds": history_span,
            "ready": complete,
        })
    return coverage


def predict_air_state(samples, now_ts, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Predict air temperature and RH from timestamped measured samples.

    Samples are mappings with Unix ``ts`` seconds, ``temp`` °C, and ``rh`` %RH.
    Invalid readings are ignored. Only the latest valid sample per 30-minute
    bucket is used. A horizon is emitted only after its required consecutive
    bucket run is complete (e.g. +6 h requires 25 half-hour buckets).
    """
    points = _sample_history(samples)
    count = len(points)
    latest_ts = points[-1][0] if points else None
    span = points[-1][0] - points[0][0] if count else 0
    coverage = _horizon_coverage(points)
    if not points:
        return _empty("warming_up")
    if latest_ts > now_ts or now_ts - latest_ts > max_age_seconds:
        result = _empty("stale", count, span, latest_ts)
        result["horizon_coverage"] = coverage
        return result

    last_ts, last_temp, last_rh = points[-1]
    last_vapor = last_rh * saturation_vapor_pressure_hpa(last_temp) / 100.0
    age_hours = max(0.0, now_ts - last_ts) / 3600.0
    forecasts = []
    for i, horizon in enumerate(HORIZONS_HOURS):
        diagnostic = coverage[i]
        required_intervals = diagnostic["required_bucket_count"] - 1
        first_bucket = math.floor(last_ts / SAMPLE_PERIOD_SECONDS) - required_intervals
        if not diagnostic["ready"]:
            continue
        window = [point for point in points
                  if first_bucket <= math.floor(
                      point[0] / SAMPLE_PERIOD_SECONDS) <=
                      math.floor(last_ts / SAMPLE_PERIOD_SECONDS)]
        # Bucket centers avoid exaggerated slopes when consecutive captures
        # happen on opposite edges of their nominal half-hour intervals.
        temp_slope = _theil_sen_per_hour(
            [(_bucket_center(ts), temp) for ts, temp, _rh in window])
        vapor_points = [
            (_bucket_center(ts),
             rh * saturation_vapor_pressure_hpa(temp) / 100.0)
            for ts, temp, rh in window
        ]
        vapor_slope = _theil_sen_per_hour(vapor_points)
        elapsed_h = age_hours + horizon
        temp = max(MIN_TEMP_C, min(MAX_TEMP_C,
                                   last_temp + temp_slope * elapsed_h))
        vapor = max(0.0, last_vapor + vapor_slope * elapsed_h)
        saturation = saturation_vapor_pressure_hpa(temp)
        vapor = min(vapor, saturation)
        rh = vapor / saturation * 100.0 if saturation > 0.0 else 0.0
        forecasts.append({
            "hour": horizon,
            "ts": int(now_ts + horizon * 3600),
            "temperature_c": temp,
            "rh_pct": rh,
            "vapor_pressure_hpa": vapor,
            "temperature_slope_c_per_h": temp_slope,
            "vapor_pressure_slope_hpa_per_h": vapor_slope,
            "history_span_seconds": int(window[-1][0] - window[0][0]),
            "bucket_span_seconds": diagnostic["bucket_span_seconds"],
            "sample_count": len(window),
        })
    return {
        "status": "ready" if forecasts else "warming_up",
        "sample_count": count,
        "span_seconds": span,
        "latest_ts": latest_ts,
        "temperature_slope_c_per_h": (forecasts[0]["temperature_slope_c_per_h"]
                                        if forecasts else None),
        "vapor_pressure_slope_hpa_per_h": (forecasts[0]["vapor_pressure_slope_hpa_per_h"]
                                            if forecasts else None),
        "forecast": forecasts,
        "valid_horizons": [point["hour"] for point in forecasts],
        "horizon_coverage": coverage,
    }
