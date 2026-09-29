"""Conservative cadence policy driven by validated probe-air forecasts.

This module is deliberately independent from persistence and AutoLink Wire.
The station decides a desired normal interval; the existing versioned settings
response transports it to S3. A missing or unvalidated forecast is never
treated as evidence that conditions are safe.
"""
import math


WATCH_INTERVAL_CAP_SECONDS = 60
SAFE_FORECASTS_TO_RECOVER = 3

_SEVERITY = {"normal": 0, "watch": 1, "risk": 2}


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _current_state(state):
    result = dict(state or {})
    mode = result.get("mode")
    if mode not in _SEVERITY:
        mode = "normal"
    try:
        safe_reports = max(0, int(result.get("safe_reports", 0)))
    except (TypeError, ValueError, OverflowError):
        safe_reports = 0
    result["mode"] = mode
    result["safe_reports"] = safe_reports
    return result


def _point_crossings(point, thresholds):
    hour = _finite_number(point.get("hour"))
    if hour is None or hour < 0:
        return []
    crossings = []
    checks = (
        ("temperature_c", "temp_high", "temperature_high", "high"),
        ("temperature_c", "temp_low", "temperature_low", "low"),
        ("rh_pct", "rh_high", "humidity_high", "high"),
        ("rh_pct", "rh_low", "humidity_low", "low"),
    )
    for measurement_key, threshold_key, reason, direction in checks:
        measured = _finite_number(point.get(measurement_key))
        threshold = _finite_number(thresholds.get(threshold_key))
        if measured is None or threshold is None:
            continue
        crossed = measured > threshold if direction == "high" else measured < threshold
        if crossed:
            crossings.append((hour, reason))
    return crossings


def advance_weather_cadence_state(state, predictions, thresholds, now_ts=None):
    """Advance persisted cadence state using independent model predictions.

    A risk-level crossing is a validated threshold crossing at +1 hour. A
    crossing at a later available horizon is watch-level. If a probe has a
    missing/unvalidated model, valid crossings from other probes can still
    escalate the policy, but the incomplete set cannot count toward recovery.
    """
    result = _current_state(state)
    predictions = list(predictions or [])
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    ready = [item for item in predictions
             if isinstance(item, dict) and item.get("status") == "ready"]
    complete = bool(predictions) and len(ready) == len(predictions)
    if not ready:
        result["status"] = "waiting_for_validated_forecast"
        return result

    crossings = []
    for prediction in ready:
        forecast = prediction.get("forecast")
        if not isinstance(forecast, list):
            continue
        for point in forecast:
            if isinstance(point, dict):
                crossings.extend(_point_crossings(point, thresholds))

    if crossings:
        horizon, reason = min(crossings, key=lambda item: (item[0], item[1]))
        target_mode = "risk" if horizon <= 1 else "watch"
        # Never silently de-escalate from an existing risk/watch state merely
        # because a crossing moved to a more distant forecast horizon.
        if _SEVERITY[target_mode] >= _SEVERITY[result["mode"]]:
            result["mode"] = target_mode
            result["horizon_hour"] = int(horizon)
            result["reason"] = reason
        result["safe_reports"] = 0
        result["status"] = "validated_limit_crossing"
        if now_ts is not None:
            try:
                result["last_valid_ts"] = int(now_ts)
            except (TypeError, ValueError, OverflowError):
                pass
        return result

    if not complete:
        result["status"] = "waiting_for_all_probe_forecasts"
        return result

    if now_ts is not None:
        try:
            result["last_valid_ts"] = int(now_ts)
        except (TypeError, ValueError, OverflowError):
            pass
    if result["mode"] == "normal":
        result["safe_reports"] = 0
        result["status"] = "validated_safe_forecast"
        result.pop("horizon_hour", None)
        result.pop("reason", None)
        return result

    result["safe_reports"] += 1
    if result["safe_reports"] >= SAFE_FORECASTS_TO_RECOVER:
        result["mode"] = "normal"
        result["safe_reports"] = 0
        result.pop("horizon_hour", None)
        result.pop("reason", None)
        result["status"] = "recovered_after_validated_safe_forecasts"
    else:
        result["status"] = "awaiting_safe_forecasts_to_recover"
    return result


def effective_normal_interval(mode, base_interval, fast_interval):
    """Return a valid normal cadence while preserving the configured fast tier."""
    base = max(1, int(base_interval))
    fast = max(1, min(base, int(fast_interval)))
    if mode == "risk":
        return fast
    if mode == "watch":
        return max(fast, min(base, WATCH_INTERVAL_CAP_SECONDS))
    return base
