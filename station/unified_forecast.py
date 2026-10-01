"""Shared, compact MLP for probe-adjacent air-state prediction.

The portable C++ implementation in master/unified_forecast_model.cpp uses the
same feature order, parameter order, activations, and physical output bounds.
Weather values are gridded model context, not on-site measurements.
"""
import math
import random

MODEL_ID = "grainsilo-air-mlp-v1"
MODEL_SCHEMA = 1
FEATURE_COUNT = 10
HIDDEN1_COUNT = 4
HIDDEN2_COUNT = 3
OUTPUT_COUNT = 6
INPUT_MEAN_COUNT = FEATURE_COUNT
INPUT_SCALE_COUNT = FEATURE_COUNT
OUTPUT_SCALE_COUNT = OUTPUT_COUNT
TRAINABLE_PARAMETER_COUNT = (
    HIDDEN1_COUNT * FEATURE_COUNT + HIDDEN1_COUNT +
    HIDDEN2_COUNT * HIDDEN1_COUNT + HIDDEN2_COUNT +
    OUTPUT_COUNT * HIDDEN2_COUNT + OUTPUT_COUNT
)
PARAMETER_COUNT = (INPUT_MEAN_COUNT + INPUT_SCALE_COUNT +
                   TRAINABLE_PARAMETER_COUNT + OUTPUT_SCALE_COUNT)
HORIZONS_HOURS = (1, 3, 6)
OUTPUT_NAMES = tuple(name for hour in HORIZONS_HOURS
                     for name in (f"temperature_{hour}h", f"vapor_{hour}h"))
MAX_AGE_SECONDS = 2700
MAX_TEMP_C = 125.0
MIN_TEMP_C = -40.0
MAX_TRAINING_HORIZON_SECONDS = 6 * 3600
MIN_TRAINING_ROWS = 60
MIN_VALIDATION_ROWS = 24


def saturation_vapor_pressure_hpa(temp_c):
    temp_c = float(temp_c)
    denominator = 243.12 + temp_c
    if not math.isfinite(temp_c) or abs(denominator) < 1e-9:
        return 0.0
    return 6.112 * math.exp((17.62 * temp_c) / denominator)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _air(sample):
    if not isinstance(sample, dict):
        return None
    ts = _finite(sample.get("ts"))
    temp = _finite(sample.get("temp", sample.get("temperature_c")))
    rh = _finite(sample.get("rh", sample.get("rh_pct")))
    if (ts is None or temp is None or rh is None or
            not MIN_TEMP_C <= temp <= MAX_TEMP_C or not 0.0 <= rh <= 100.0):
        return None
    vapor = rh * saturation_vapor_pressure_hpa(temp) / 100.0
    return {"ts": int(ts), "temp": temp, "rh": rh, "vapor": vapor}


def _weather(sample, now_ts=None):
    if not isinstance(sample, dict):
        return None
    ts = _finite(sample.get("ts"))
    temp = _finite(sample.get("temperature_c", sample.get("temp_c")))
    rh = _finite(sample.get("rh_pct", sample.get("relative_humidity_pct")))
    if (ts is None or temp is None or rh is None or
            not MIN_TEMP_C <= temp <= MAX_TEMP_C or not 0.0 <= rh <= 100.0):
        return None
    if now_ts is not None and (ts > now_ts or now_ts - ts > 3 * 3600):
        return None
    return {"ts": int(ts), "temp": temp, "rh": rh,
            "vapor": rh * saturation_vapor_pressure_hpa(temp) / 100.0}


def build_feature_vector(current, previous_1h=None, peers=None, weather=None):
    """Build the fixed 10-value input vector used on both runtimes.

    Features: node temp/vapor, one-hour temp/vapor delta, peer-mean-minus-node
    temp/vapor, current gridded-weather temp/vapor, weather-present flag, and
    peer count normalized to [0, 1] for up to two peers.
    """
    node = _air(current)
    if node is None:
        raise ValueError("current node reading is invalid")
    previous = _air(previous_1h)
    if previous is not None and previous["ts"] >= node["ts"]:
        previous = None
    peer_rows = [value for value in (_air(item) for item in (peers or []))
                 if value is not None and abs(value["ts"] - node["ts"]) <= 2700]
    if peer_rows:
        peer_temp = sum(item["temp"] for item in peer_rows) / len(peer_rows)
        peer_vapor = sum(item["vapor"] for item in peer_rows) / len(peer_rows)
    else:
        peer_temp, peer_vapor = node["temp"], node["vapor"]
    outside = _weather(weather, now_ts=node["ts"])
    values = [
        node["temp"], node["vapor"],
        node["temp"] - previous["temp"] if previous else 0.0,
        node["vapor"] - previous["vapor"] if previous else 0.0,
        peer_temp - node["temp"], peer_vapor - node["vapor"],
        outside["temp"] if outside else 0.0,
        outside["vapor"] if outside else 0.0,
        1.0 if outside else 0.0,
        min(1.0, len(peer_rows) / 2.0),
    ]
    return values


def _empty(status, reason, latest_ts=None):
    return {"status": status, "reason": reason, "model_id": MODEL_ID,
            "model_version": 0, "source": "persistence_baseline",
            "latest_ts": latest_ts, "validated_horizons": [], "forecast": []}


def validation_mask(model):
    """Return the six-bit output mask shared by Station and the S3 firmware."""
    names = set(model.get("validated_outputs") or []) if isinstance(model, dict) else set()
    if not names and isinstance(model, dict):
        for hour in model.get("validated_horizons") or []:
            names.update((f"temperature_{hour}h", f"vapor_{hour}h"))
    return sum(1 << index for index, name in enumerate(OUTPUT_NAMES)
               if name in names)


def _latest_by_address(histories_by_addr, now_ts):
    result = {}
    for raw_addr, rows in (histories_by_addr or {}).items():
        try:
            addr = int(raw_addr)
        except (TypeError, ValueError):
            continue
        valid = [item for item in (_air(row) for row in (rows or []))
                 if item is not None and item["ts"] <= now_ts]
        if valid:
            valid.sort(key=lambda item: item["ts"])
            result[addr] = valid
    return result


def _forward(model, inputs):
    a1 = [math.tanh(model["b1"][i] + sum(
        model["w1"][i][j] * inputs[j] for j in range(FEATURE_COUNT)))
          for i in range(HIDDEN1_COUNT)]
    a2 = [math.tanh(model["b2"][i] + sum(
        model["w2"][i][j] * a1[j] for j in range(HIDDEN1_COUNT)))
          for i in range(HIDDEN2_COUNT)]
    return [model["b3"][i] + sum(
        model["w3"][i][j] * a2[j] for j in range(HIDDEN2_COUNT))
            for i in range(OUTPUT_COUNT)]


def predict_unified_forecast(model, histories_by_addr, addr, weather=None,
                             now_ts=None, max_age_seconds=MAX_AGE_SECONDS):
    """Predict one node using a trained model, or immediate persistence fallback."""
    now_ts = int(now_ts if now_ts is not None else __import__("time").time())
    try:
        addr = int(addr)
    except (TypeError, ValueError):
        return _empty("unavailable", "invalid_node_address")
    histories = _latest_by_address(histories_by_addr, now_ts)
    own = histories.get(addr, [])
    if not own:
        return _empty("warming_up", "no_valid_measurement")
    current = own[-1]
    if now_ts - current["ts"] > max_age_seconds:
        return _empty("stale", "probe_measurement_stale", current["ts"])

    target_previous_ts = current["ts"] - 3600
    previous_candidates = [row for row in own[:-1]
                           if abs(row["ts"] - target_previous_ts) <= 1800]
    previous = (min(previous_candidates,
                    key=lambda row: abs(row["ts"] - target_previous_ts))
                if previous_candidates else None)
    peers = [rows[-1] for other_addr, rows in histories.items()
             if other_addr != addr and now_ts - rows[-1]["ts"] <= max_age_seconds]
    features = build_feature_vector(current, previous, peers, weather)
    current_temp = current["temp"]
    current_vapor = current["vapor"]
    model_ok = isinstance(model, dict) and model.get("model_id") == MODEL_ID
    if model_ok:
        try:
            if (len(model["input_mean"]) != FEATURE_COUNT or
                    len(model["input_scale"]) != FEATURE_COUNT or
                    len(model["output_scale"]) != OUTPUT_COUNT):
                raise ValueError("bad model vector dimensions")
            normalized = [(features[i] - float(model["input_mean"][i])) /
                          max(1e-8, float(model["input_scale"][i]))
                          for i in range(FEATURE_COUNT)]
            if not all(math.isfinite(value) for value in normalized):
                raise ValueError("bad normalized input")
            delta = _forward(model, normalized)
            delta = [delta[i] * float(model["output_scale"][i])
                     for i in range(OUTPUT_COUNT)]
            if not all(math.isfinite(value) for value in delta):
                raise ValueError("bad model output")
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            model_ok = False
    if not model_ok:
        delta = [0.0] * OUTPUT_COUNT
    validated = [hour for hour in HORIZONS_HOURS
                 if model_ok and hour in (model.get("validated_horizons") or [])]
    validated_outputs = set(model.get("validated_outputs") or []) if model_ok else set()
    if model_ok and not validated_outputs:
        for hour in model.get("validated_horizons") or []:
            validated_outputs.update((f"temperature_{hour}h", f"vapor_{hour}h"))
    points = []
    for index, hour in enumerate(HORIZONS_HOURS):
        temp_validated = f"temperature_{hour}h" in validated_outputs
        vapor_validated = f"vapor_{hour}h" in validated_outputs
        candidate_temperature = max(MIN_TEMP_C, min(
            MAX_TEMP_C, current_temp + delta[index * 2]))
        candidate_vapor = max(0.0, current_vapor + delta[index * 2 + 1])
        candidate_saturation = saturation_vapor_pressure_hpa(
            candidate_temperature)
        candidate_vapor = min(candidate_vapor, candidate_saturation)
        candidate_rh = (max(0.0, min(100.0,
                        candidate_vapor / candidate_saturation * 100.0))
                        if candidate_saturation else 0.0)
        temp_delta = delta[index * 2] if temp_validated else 0.0
        vapor_delta = delta[index * 2 + 1] if vapor_validated else 0.0
        temperature = max(MIN_TEMP_C, min(MAX_TEMP_C,
            current_temp + temp_delta))
        vapor = max(0.0, current_vapor + vapor_delta)
        saturation = saturation_vapor_pressure_hpa(temperature)
        vapor = min(vapor, saturation)
        rh = max(0.0, min(100.0, vapor / saturation * 100.0)) if saturation else 0.0
        points.append({"hour": hour, "ts": now_ts + hour * 3600,
                       "temperature_c": temperature, "temp": temperature,
                       "rh_pct": rh, "rh": rh, "vapor_pressure_hpa": vapor,
                       "candidate_available": model_ok,
                       "candidate_temperature_c": (candidate_temperature
                                                   if model_ok else None),
                       "candidate_rh_pct": candidate_rh if model_ok else None,
                       "validated": temp_validated and vapor_validated,
                       "temperature_validated": temp_validated,
                       "rh_validated": vapor_validated})
    return {
        "status": ("ready" if validated_outputs else
                   "candidate" if model_ok else "baseline"),
        "reason": None if model_ok else "trained_model_unavailable",
        "model_id": MODEL_ID,
        "model_version": int(model.get("version", 0)) if model_ok else 0,
        "source": "unified_mlp" if model_ok else "persistence_baseline",
        "latest_ts": current["ts"],
        "weather_input": "gridded_weather" if features[8] else "unavailable",
        "sample_count": len(own),
        "validated_horizons": validated,
        "validated_outputs": sorted(validated_outputs),
        "validation": model.get("metrics", {}) if model_ok else {},
        "forecast": points,
    }


def _empty_model(version=1):
    return {
        "model_id": MODEL_ID,
        "schema": MODEL_SCHEMA,
        "version": int(version),
        "input_mean": [0.0] * FEATURE_COUNT,
        "input_scale": [1.0] * FEATURE_COUNT,
        "w1": [[0.0] * FEATURE_COUNT for _ in range(HIDDEN1_COUNT)],
        "b1": [0.0] * HIDDEN1_COUNT,
        "w2": [[0.0] * HIDDEN1_COUNT for _ in range(HIDDEN2_COUNT)],
        "b2": [0.0] * HIDDEN2_COUNT,
        "w3": [[0.0] * HIDDEN2_COUNT for _ in range(OUTPUT_COUNT)],
        "b3": [0.0] * OUTPUT_COUNT,
        "output_scale": [1.0] * OUTPUT_COUNT,
        "validated_horizons": [],
    }


def _trainable_blocks(model):
    return ([row for row in model["w1"]] + [model["b1"]] +
            [row for row in model["w2"]] + [model["b2"]] +
            [row for row in model["w3"]] + [model["b3"]])


def _target_norm(row, model):
    return [float(row["targets"][i]) / model["output_scale"][i]
            for i in range(OUTPUT_COUNT)]


def _forward_and_gradients(model, inputs, target):
    a1 = [math.tanh(model["b1"][i] + sum(
        model["w1"][i][j] * inputs[j] for j in range(FEATURE_COUNT)))
          for i in range(HIDDEN1_COUNT)]
    a2 = [math.tanh(model["b2"][i] + sum(
        model["w2"][i][j] * a1[j] for j in range(HIDDEN1_COUNT)))
          for i in range(HIDDEN2_COUNT)]
    output = [model["b3"][i] + sum(
        model["w3"][i][j] * a2[j] for j in range(HIDDEN2_COUNT))
              for i in range(OUTPUT_COUNT)]
    d3 = [(output[i] - target[i]) / OUTPUT_COUNT for i in range(OUTPUT_COUNT)]
    gw3 = [[d3[i] * a2[j] for j in range(HIDDEN2_COUNT)]
           for i in range(OUTPUT_COUNT)]
    gb3 = list(d3)
    da2 = [sum(model["w3"][i][j] * d3[i]
               for i in range(OUTPUT_COUNT)) for j in range(HIDDEN2_COUNT)]
    d2 = [da2[i] * (1.0 - a2[i] * a2[i]) for i in range(HIDDEN2_COUNT)]
    gw2 = [[d2[i] * a1[j] for j in range(HIDDEN1_COUNT)]
           for i in range(HIDDEN2_COUNT)]
    gb2 = list(d2)
    da1 = [sum(model["w2"][i][j] * d2[i]
               for i in range(HIDDEN2_COUNT)) for j in range(HIDDEN1_COUNT)]
    d1 = [da1[i] * (1.0 - a1[i] * a1[i]) for i in range(HIDDEN1_COUNT)]
    gw1 = [[d1[i] * inputs[j] for j in range(FEATURE_COUNT)]
           for i in range(HIDDEN1_COUNT)]
    gb1 = list(d1)
    gradients = [*gw1, gb1, *gw2, gb2, *gw3, gb3]
    return gradients


def _normalise_features(rows, model):
    for index in range(FEATURE_COUNT):
        values = [float(row["features"][index]) for row in rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std = math.sqrt(max(0.0, variance))
        model["input_mean"][index] = mean
        model["input_scale"][index] = std if std > 1e-8 else 1.0


def _normalise_targets(rows, model):
    floors = [0.25, 0.5, 0.25, 0.5, 0.25, 0.5]
    for index in range(OUTPUT_COUNT):
        values = [float(row["targets"][index]) for row in rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        model["output_scale"][index] = max(floors[index], math.sqrt(max(0.0, variance)))


def _normalised_inputs(row, model):
    return [(float(row["features"][i]) - model["input_mean"][i]) /
            model["input_scale"][i] for i in range(FEATURE_COUNT)]


def _prediction_delta(row, model):
    return [value * model["output_scale"][i]
            for i, value in enumerate(_forward(model, _normalised_inputs(row, model)))]


def _skill(model, validation):
    metrics = {}
    validated_horizons = []
    validated_outputs = []
    for h_index, hour in enumerate(HORIZONS_HOURS):
        temp_errors, temp_baseline = [], []
        rh_errors, rh_baseline = [], []
        for row in validation:
            prediction = _prediction_delta(row, model)
            current_temp, current_vapor = map(float, row["current"])
            temp_target = current_temp + float(row["targets"][h_index * 2])
            vapor_target = current_vapor + float(row["targets"][h_index * 2 + 1])
            predicted_temp = max(MIN_TEMP_C, min(
                MAX_TEMP_C, current_temp + prediction[h_index * 2]))
            temp_errors.append(abs(predicted_temp - temp_target))
            temp_baseline.append(abs(current_temp - temp_target))
            current_saturation = saturation_vapor_pressure_hpa(current_temp)
            target_saturation = saturation_vapor_pressure_hpa(temp_target)
            predicted_saturation = saturation_vapor_pressure_hpa(predicted_temp)
            actual_rh = (max(0.0, min(100.0,
                         vapor_target / target_saturation * 100.0))
                         if target_saturation > 0.0 else 0.0)
            baseline_rh = (max(0.0, min(100.0,
                           current_vapor / current_saturation * 100.0))
                           if current_saturation > 0.0 else 0.0)
            predicted_vapor = max(0.0, current_vapor + prediction[h_index * 2 + 1])
            predicted_vapor = min(predicted_vapor, predicted_saturation)
            predicted_rh = (max(0.0, min(100.0,
                            predicted_vapor / predicted_saturation * 100.0))
                            if predicted_saturation > 0.0 else 0.0)
            rh_errors.append(abs(predicted_rh - actual_rh))
            rh_baseline.append(abs(baseline_rh - actual_rh))
        temp_mae = sum(temp_errors) / len(temp_errors)
        temp_base = sum(temp_baseline) / len(temp_baseline)
        rh_mae = sum(rh_errors) / len(rh_errors)
        rh_base = sum(rh_baseline) / len(rh_baseline)
        temp_skill = ((temp_base - temp_mae) / temp_base
                      if temp_base > 1e-8 else None)
        rh_skill = ((rh_base - rh_mae) / rh_base
                    if rh_base > 1e-8 else None)
        temp_validated = temp_skill is not None and temp_skill >= 0.0
        rh_validated = rh_skill is not None and rh_skill >= 0.0
        if temp_validated:
            validated_outputs.append(f"temperature_{hour}h")
        if rh_validated:
            validated_outputs.append(f"vapor_{hour}h")
        validated = temp_validated and rh_validated
        if validated:
            validated_horizons.append(hour)
        metrics[str(hour)] = {
            "temperature_mae_c": temp_mae,
            "temperature_persistence_mae_c": temp_base,
            "temperature_skill": temp_skill,
            "rh_mae_pct_points": rh_mae,
            "rh_persistence_mae_pct_points": rh_base,
            "rh_skill": rh_skill,
            "temperature_validated": temp_validated,
            "rh_validated": rh_validated,
            "validated": validated,
        }
    return metrics, validated_horizons, validated_outputs


def train_unified_model(rows, *, epochs=80, seed=20260930,
                        holdout_fraction=0.2, learning_rate=0.01,
                        batch_size=32, version=1):
    """Train using ordered rows and a purged chronological validation tail.

    ``rows`` contain timestamp, 10 features, current [temperature, vapor],
    and six residual targets (temp/vapor for +1/+3/+6 h).
    """
    cleaned = []
    for row in rows or []:
        try:
            ts = int(row["ts"])
            features = [float(value) for value in row["features"]]
            current = [float(value) for value in row["current"]]
            targets = [float(value) for value in row["targets"]]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (len(features) != FEATURE_COUNT or len(current) != 2 or
                len(targets) != OUTPUT_COUNT or
                not all(math.isfinite(value) for value in features + current + targets)):
            continue
        cleaned.append({"ts": ts, "features": features,
                        "current": current, "targets": targets})
    cleaned.sort(key=lambda row: row["ts"])
    distinct_times = sorted({row["ts"] for row in cleaned})
    if len(distinct_times) < 2:
        raise ValueError("not enough distinct forecast timestamps")
    start_ts, end_ts = distinct_times[0], distinct_times[-1]
    cutoff = start_ts + int((end_ts - start_ts) * (1.0 - holdout_fraction))
    training = [row for row in cleaned
                if row["ts"] < cutoff - MAX_TRAINING_HORIZON_SECONDS]
    validation = [row for row in cleaned if row["ts"] >= cutoff]
    if len(training) < MIN_TRAINING_ROWS or len(validation) < MIN_VALIDATION_ROWS:
        raise ValueError("insufficient rows for chronological train/validation split")

    model = _empty_model(version)
    _normalise_features(training, model)
    _normalise_targets(training, model)
    rng = random.Random(int(seed))
    scale1 = math.sqrt(2.0 / (FEATURE_COUNT + HIDDEN1_COUNT))
    scale2 = math.sqrt(2.0 / (HIDDEN1_COUNT + HIDDEN2_COUNT))
    scale3 = math.sqrt(2.0 / (HIDDEN2_COUNT + OUTPUT_COUNT))
    model["w1"] = [[rng.gauss(0.0, scale1) for _ in range(FEATURE_COUNT)]
                   for _ in range(HIDDEN1_COUNT)]
    model["w2"] = [[rng.gauss(0.0, scale2) for _ in range(HIDDEN1_COUNT)]
                   for _ in range(HIDDEN2_COUNT)]
    model["w3"] = [[rng.gauss(0.0, scale3) for _ in range(HIDDEN2_COUNT)]
                   for _ in range(OUTPUT_COUNT)]

    blocks = _trainable_blocks(model)
    first_moments = [[0.0] * len(block) for block in blocks]
    second_moments = [[0.0] * len(block) for block in blocks]
    step = 0
    epochs = max(1, min(500, int(epochs)))
    batch_size = max(1, min(256, int(batch_size)))
    for _epoch in range(epochs):
        order = list(range(len(training)))
        rng.shuffle(order)
        for offset in range(0, len(order), batch_size):
            batch = [training[index] for index in order[offset:offset + batch_size]]
            gradients = [[0.0] * len(block) for block in blocks]
            for row in batch:
                inputs = _normalised_inputs(row, model)
                target = _target_norm(row, model)
                sample_gradients = _forward_and_gradients(model, inputs, target)
                for block_index, (target_grad, source_grad) in enumerate(
                        zip(gradients, sample_gradients)):
                    for value_index, value in enumerate(source_grad):
                        target_grad[value_index] += value
            step += 1
            beta1, beta2, epsilon = 0.9, 0.999, 1e-8
            for block_index, (parameter, gradient) in enumerate(zip(blocks, gradients)):
                for value_index in range(len(parameter)):
                    grad = gradient[value_index] / len(batch)
                    grad = max(-5.0, min(5.0, grad))
                    first_moments[block_index][value_index] = (
                        beta1 * first_moments[block_index][value_index] +
                        (1.0 - beta1) * grad)
                    second_moments[block_index][value_index] = (
                        beta2 * second_moments[block_index][value_index] +
                        (1.0 - beta2) * grad * grad)
                    m_hat = first_moments[block_index][value_index] / (1.0 - beta1 ** step)
                    v_hat = second_moments[block_index][value_index] / (1.0 - beta2 ** step)
                    parameter[value_index] -= (learning_rate * m_hat /
                                               (math.sqrt(v_hat) + epsilon))

    metrics, validated, validated_outputs = _skill(model, validation)
    model.update({
        "validated_horizons": validated,
        "validated_outputs": validated_outputs,
        "metrics": metrics,
        "training_count": len(training),
        "validation_count": len(validation),
        "training_start_ts": training[0]["ts"],
        "max_training_ts": training[-1]["ts"],
        "validation_start_ts": validation[0]["ts"],
        "trained_from_ts": start_ts,
        "trained_to_ts": end_ts,
        "epochs": epochs,
    })
    return model


def _trainable_values(model):
    return [value for block in _trainable_blocks(model) for value in block]


def pack_model_parameters(model):
    """Flatten normalizers and MLP parameters for the S3 HTTP/NVS contract."""
    if not isinstance(model, dict) or model.get("model_id") != MODEL_ID:
        raise ValueError("unsupported forecast model")
    values = ([float(value) for value in model["input_mean"]] +
              [float(value) for value in model["input_scale"]] +
              _trainable_values(model) +
              [float(value) for value in model["output_scale"]])
    if (len(values) != PARAMETER_COUNT or
            not all(math.isfinite(x) and abs(x) <= 10000.0 for x in values)):
        raise ValueError("invalid forecast parameter vector")
    if any(value <= 0.0 for value in model["input_scale"] + model["output_scale"]):
        raise ValueError("forecast scales must be positive")
    return values


def unpack_model_parameters(parameters, *, version=1, validated_horizons=None):
    """Restore one fixed-size parameter vector received from Station."""
    values = [float(value) for value in parameters]
    if (len(values) != PARAMETER_COUNT or
            not all(math.isfinite(x) and abs(x) <= 10000.0 for x in values)):
        raise ValueError("invalid forecast parameter vector")
    cursor = 0

    def take(count):
        nonlocal cursor
        result = values[cursor:cursor + count]
        cursor += count
        return result

    model = _empty_model(version)
    model["input_mean"] = take(FEATURE_COUNT)
    model["input_scale"] = take(FEATURE_COUNT)
    model["w1"] = [take(FEATURE_COUNT) for _ in range(HIDDEN1_COUNT)]
    model["b1"] = take(HIDDEN1_COUNT)
    model["w2"] = [take(HIDDEN1_COUNT) for _ in range(HIDDEN2_COUNT)]
    model["b2"] = take(HIDDEN2_COUNT)
    model["w3"] = [take(HIDDEN2_COUNT) for _ in range(OUTPUT_COUNT)]
    model["b3"] = take(OUTPUT_COUNT)
    model["output_scale"] = take(OUTPUT_COUNT)
    if any(value <= 0.0 for value in model["input_scale"] + model["output_scale"]):
        raise ValueError("forecast scales must be positive")
    if cursor != PARAMETER_COUNT:
        raise ValueError("forecast parameter vector was not fully consumed")
    model["validated_horizons"] = [hour for hour in (validated_horizons or [])
                                   if hour in HORIZONS_HOURS]
    return model


def apply_validation_mask(model, mask):
    """Attach output-level holdout results received over the bounded HTTP contract."""
    mask = int(mask)
    if mask < 0 or mask > (1 << OUTPUT_COUNT) - 1:
        raise ValueError("invalid forecast validation mask")
    model["validated_outputs"] = [name for index, name in enumerate(OUTPUT_NAMES)
                                  if mask & (1 << index)]
    model["validated_horizons"] = [hour for index, hour in enumerate(HORIZONS_HOURS)
                                   if (mask & (1 << (index * 2))) and
                                   (mask & (1 << (index * 2 + 1)))]
    return model
