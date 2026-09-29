"""Locally calibrated weather-assisted estimates for probe-adjacent air.

The weather input is a gridded model value, not a warehouse weather-station
measurement. The model is withheld until both temperature and vapor-pressure
channels have enough paired history and pass a chronological holdout against
a persistence baseline. This is a candidate operational reference, not CFD or
grain-moisture prediction.
"""
import bisect
import math

try:
    from .forecast_model import saturation_vapor_pressure_hpa
except ImportError:  # Station loads this file directly from its app directory.
    from forecast_model import saturation_vapor_pressure_hpa

MIN_TRANSITIONS = 72
VALIDATION_TRANSITIONS = 24
MIN_SKILL_SCORE = 0.05
MAX_PAIR_SKEW_SECONDS = 1800
MIN_TRANSITION_SECONDS = 2700
MAX_TRANSITION_SECONDS = 4500
MAX_FORECAST_HOURS = 6
FORECAST_HORIZONS = (1, 3, 6)
MIN_TEMP_C = -40.0
MAX_TEMP_C = 125.0


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _normalise_probe(samples):
    result = []
    for sample in samples or []:
        ts = _number(sample.get("ts"))
        temp = _number(sample.get("temp"))
        rh = _number(sample.get("rh"))
        if (ts is None or temp is None or rh is None or
                not MIN_TEMP_C <= temp <= MAX_TEMP_C or not 0.0 <= rh <= 100.0):
            continue
        result.append((int(ts), temp,
                       rh * saturation_vapor_pressure_hpa(temp) / 100.0))
    result.sort(key=lambda row: row[0])
    return result


def _normalise_weather(samples):
    by_ts = {}
    for sample in samples or []:
        ts = _number(sample.get("ts"))
        temp = _number(sample.get("temperature_c", sample.get("temp_c")))
        rh = _number(sample.get("rh_pct", sample.get("relative_humidity_pct")))
        if (ts is None or temp is None or rh is None or
                not MIN_TEMP_C <= temp <= MAX_TEMP_C or not 0.0 <= rh <= 100.0):
            continue
        by_ts[int(ts)] = (temp, rh * saturation_vapor_pressure_hpa(temp) / 100.0)
    return [(ts, values[0], values[1]) for ts, values in sorted(by_ts.items())]


def _pair_probe_and_weather(probe, weather):
    if not probe or not weather:
        return []
    probe_times = [row[0] for row in probe]
    used = set()
    paired = []
    for weather_ts, outside_temp, outside_vapor in weather:
        index = bisect.bisect_left(probe_times, weather_ts)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(probe)]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda i: abs(probe_times[i] - weather_ts))
        probe_ts, probe_temp, probe_vapor = probe[nearest]
        if (abs(probe_ts - weather_ts) > MAX_PAIR_SKEW_SECONDS or
                probe_ts in used):
            continue
        used.add(probe_ts)
        paired.append((weather_ts, probe_temp, probe_vapor,
                       outside_temp, outside_vapor))
    paired.sort(key=lambda row: row[0])
    return paired


def _transitions(paired, channel):
    if channel == "temperature":
        probe_index, outside_index = 1, 3
    else:
        probe_index, outside_index = 2, 4
    result = []
    for left, right in zip(paired, paired[1:]):
        elapsed = right[0] - left[0]
        if MIN_TRANSITION_SECONDS <= elapsed <= MAX_TRANSITION_SECONDS:
            result.append((left[probe_index], left[outside_index],
                           right[probe_index]))
    return result


def _solve(matrix, vector):
    size = len(vector)
    work = [list(matrix[row]) + [vector[row]] for row in range(size)]
    scale = max((abs(value) for row in matrix for value in row), default=0.0)
    if not scale:
        return None
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= scale * 1e-10:
            return None
        work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        work[column] = [value / divisor for value in work[column]]
        for row in range(size):
            if row == column:
                continue
            factor = work[row][column]
            work[row] = [value - factor * base
                         for value, base in zip(work[row], work[column])]
    result = [work[index][-1] for index in range(size)]
    return result if all(math.isfinite(value) for value in result) else None


def _fit(transitions):
    if len(transitions) < 4:
        return None
    x_mean = sum(row[0] for row in transitions) / len(transitions)
    u_mean = sum(row[1] for row in transitions) / len(transitions)
    x_std = math.sqrt(sum((row[0] - x_mean) ** 2 for row in transitions) /
                       len(transitions))
    u_std = math.sqrt(sum((row[1] - u_mean) ** 2 for row in transitions) /
                       len(transitions))
    if x_std < 1e-8 or u_std < 1e-8:
        return None
    matrix = [[0.0] * 3 for _ in range(3)]
    vector = [0.0] * 3
    for x, outside, target in transitions:
        row = ((x - x_mean) / x_std, (outside - u_mean) / u_std, 1.0)
        for i in range(3):
            vector[i] += row[i] * target
            for j in range(3):
                matrix[i][j] += row[i] * row[j]
    beta = _solve(matrix, vector)
    if beta is None:
        return None
    a = beta[0] / x_std
    b = beta[1] / u_std
    c = beta[2] - a * x_mean - b * u_mean
    return (a, b, c) if all(math.isfinite(value) for value in (a, b, c)) else None


def _stable(coefficients):
    if coefficients is None:
        return False
    a, b, _c = coefficients
    return -0.02 <= a <= 1.05 and -0.02 <= b <= 1.05 and a + b <= 1.08


def _calibrate(transitions):
    diagnostic = {"status": "warming_up", "sample_count": len(transitions),
                  "required_samples": MIN_TRANSITIONS,
                  "validation_samples": VALIDATION_TRANSITIONS,
                  "skill_score": None, "model_mae": None,
                  "persistence_mae": None}
    if len(transitions) < MIN_TRANSITIONS:
        diagnostic["reason"] = "insufficient_paired_history"
        return None, diagnostic
    split = len(transitions) - VALIDATION_TRANSITIONS
    training, validation = transitions[:split], transitions[split:]
    validation_coefficients = _fit(training)
    if not _stable(validation_coefficients):
        diagnostic.update({"status": "not_validated",
                           "reason": "unstable_or_unidentifiable_response"})
        return None, diagnostic
    a, b, c = validation_coefficients
    model_error = [abs((a * x + b * outside + c) - target)
                   for x, outside, target in validation]
    persistence_error = [abs(x - target) for x, _outside, target in validation]
    model_mae = sum(model_error) / len(model_error)
    baseline_mae = sum(persistence_error) / len(persistence_error)
    skill = ((baseline_mae - model_mae) / baseline_mae
             if baseline_mae > 1e-8 else None)
    diagnostic.update({"model_mae": model_mae,
                       "persistence_mae": baseline_mae,
                       "skill_score": skill})
    if skill is None or skill < MIN_SKILL_SCORE:
        diagnostic.update({"status": "not_validated",
                           "reason": "holdout_not_better_than_persistence"})
        return None, diagnostic
    coefficients = _fit(transitions)
    if not _stable(coefficients):
        diagnostic.update({"status": "not_validated",
                           "reason": "unstable_full_fit"})
        return None, diagnostic
    diagnostic.update({"status": "ready", "reason": None})
    return coefficients, diagnostic


def _forecast_weather_at(weather, target_ts):
    if not weather:
        return None
    times = [row[0] for row in weather]
    index = bisect.bisect_left(times, target_ts)
    candidates = [i for i in (index - 1, index) if 0 <= i < len(weather)]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda i: abs(times[i] - target_ts))
    if abs(times[nearest] - target_ts) > 5400:
        return None
    return weather[nearest][1], weather[nearest][2]


def predict_weather_assisted_air_state(probe_samples, weather_history,
                                       weather_forecast, now_ts):
    """Forecast probe-adjacent air with locally learned weather response.

    Fits ``x[t+1] = a*x[t] + b*weather[t] + c`` independently to measured
    temperature and vapor pressure. Both channels require at least 72 paired
    hourly transitions and a chronological 24-transition holdout with >=5%
    improvement over persistence. Forecast RH is reconstructed from predicted
    temperature and vapor pressure, never regressed as raw percent RH.
    """
    probe = _normalise_probe(probe_samples)
    history = _normalise_weather(weather_history)
    future = _normalise_weather(weather_forecast)
    paired = _pair_probe_and_weather(probe, history)
    temp_coefficients, temp_diagnostic = _calibrate(
        _transitions(paired, "temperature"))
    vapor_coefficients, vapor_diagnostic = _calibrate(
        _transitions(paired, "vapor_pressure"))
    pair_count = min(temp_diagnostic["sample_count"],
                     vapor_diagnostic["sample_count"])
    result = {
        "status": "ready", "source": "probe measurements + warehouse-location weather model",
        "model": "locally calibrated first-order weather response",
        "pair_count": pair_count,
        "temperature_calibration": temp_diagnostic,
        "vapor_pressure_calibration": vapor_diagnostic,
        "forecast": [],
        "note": ("Weather input is gridded model output, not on-site weather-station data. "
                 "One-step holdout skill is not long-term field certification; "
                 "the target is probe-adjacent air, not bulk-grain temperature or moisture."),
    }
    diagnostics = (temp_diagnostic, vapor_diagnostic)
    if any(item["status"] != "ready" for item in diagnostics):
        result["status"] = ("not_validated" if any(
            item["status"] == "not_validated" for item in diagnostics)
            else "warming_up")
        result["reason"] = ("insufficient_paired_history" if result["status"] == "warming_up"
                             else "weather_model_not_validated")
        return result
    if not probe:
        result.update({"status": "warming_up", "reason": "no_probe_measurement"})
        return result

    last_ts, temperature, vapor = probe[-1]
    if int(now_ts) < last_ts or int(now_ts) - last_ts > 2700:
        result.update({"status": "stale", "reason": "probe_measurement_stale"})
        return result
    if not future:
        result.update({"status": "unavailable", "reason": "weather_forecast_unavailable"})
        return result

    ta, tb, tc = temp_coefficients
    ea, eb, ec = vapor_coefficients
    forecasts = []
    for step in range(1, MAX_FORECAST_HOURS + 1):
        outside = _forecast_weather_at(future, int(now_ts) + (step - 1) * 3600)
        if outside is None:
            result.update({"status": "unavailable",
                           "reason": "weather_forecast_gap"})
            return result
        outside_temp, outside_vapor = outside
        temperature = max(MIN_TEMP_C, min(MAX_TEMP_C,
                                          ta * temperature + tb * outside_temp + tc))
        vapor = max(0.0, ea * vapor + eb * outside_vapor + ec)
        saturation = saturation_vapor_pressure_hpa(temperature)
        vapor = min(vapor, saturation)
        if step in FORECAST_HORIZONS:
            forecasts.append({
                "hour": step,
                "ts": int(now_ts) + step * 3600,
                "temperature_c": temperature,
                "rh_pct": vapor / saturation * 100.0 if saturation > 0 else 0.0,
                "vapor_pressure_hpa": vapor,
                "temperature_source": "probe + weather calibrated response",
                "humidity_source": "probe vapor pressure + weather calibrated response",
            })
    result["forecast"] = forecasts
    result["reason"] = None
    return result
