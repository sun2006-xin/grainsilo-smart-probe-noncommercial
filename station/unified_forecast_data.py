"""Leakage-aware historical sample preparation for the unified air model."""
import json
import math
import time

try:
    from .unified_forecast import (HORIZONS_HOURS, build_feature_vector,
                                  saturation_vapor_pressure_hpa)
except ImportError:  # Station imports sibling modules directly when frozen.
    from unified_forecast import (HORIZONS_HOURS, build_feature_vector,
                                  saturation_vapor_pressure_hpa)

BIN_SECONDS = 600
MAX_SAMPLE_AGE_SECONDS = 600
MAX_WEATHER_AGE_SECONDS = 3 * 3600
TRAINING_HISTORY_DAYS = 45


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _node_reading(node, snapshot_ts):
    if not isinstance(node, dict):
        return None
    try:
        addr = int(node.get("addr"))
    except (TypeError, ValueError):
        return None
    temp = _number(node.get("temp", node.get("t")))
    rh = _number(node.get("rh", node.get("h")))
    if (addr < 1 or addr > 247 or temp is None or rh is None or
            not -40.0 <= temp <= 125.0 or not 0.0 <= rh <= 100.0):
        return None
    try:
        status = int(node.get("status", 0))
    except (TypeError, ValueError):
        status = 1
    if status != 0:
        return None
    age_ms = _number(node.get("sample_age_ms"))
    if age_ms is not None:
        if age_ms < 0.0 or age_ms > MAX_SAMPLE_AGE_SECONDS * 1000:
            return None
        sample_ts = int(snapshot_ts - age_ms / 1000.0)
    else:
        sample_ts = int(snapshot_ts)
    sample = {"ts": sample_ts, "temp": temp, "rh": rh}
    if node.get("sample_boot_id") is not None and node.get("sample_seq") is not None:
        sample["identity"] = (str(node.get("sample_boot_id")),
                              str(node.get("sample_seq")))
    return addr, sample


def _parse_weather(rows):
    result = {}
    for row in rows or []:
        try:
            ts = int(row.get("ts", row.get("weather_ts")))
        except (TypeError, ValueError):
            continue
        temp = _number(row.get("temperature_c", row.get("temp_c")))
        rh = _number(row.get("rh_pct", row.get("relative_humidity_pct")))
        if (temp is None or rh is None or not -40.0 <= temp <= 125.0 or
                not 0.0 <= rh <= 100.0):
            continue
        result[ts] = {"ts": ts, "temperature_c": temp, "rh_pct": rh}
    return sorted(result.values(), key=lambda item: item["ts"])


def _nearest_weather(rows, target_ts):
    # Use only weather already available at inference time. A nearest-time join
    # can silently select a future observation and leak target-period context.
    eligible = [row for row in rows if row["ts"] <= target_ts]
    nearest = max(eligible, key=lambda row: row["ts"], default=None)
    if nearest is None or target_ts - nearest["ts"] > MAX_WEATHER_AGE_SECONDS:
        return None
    return nearest


def build_training_rows(snapshot_rows, weather_rows, *, warehouse_id=None,
                        now_ts=None, history_days=TRAINING_HISTORY_DAYS):
    """Create 10-minute aligned examples from snapshots and weather history.

    If sample boot/sequence metadata exists, repeated snapshot copies are
    removed before binning. Legacy rows are downsampled to the latest valid
    reading in each fixed 10-minute UTC bucket. Targets come only from probe
    measurements at +1/+3/+6 hours; weather at the forecast target is never
    read, preventing future-weather leakage.
    """
    filtered = []
    for row in snapshot_rows or []:
        if warehouse_id is not None and row.get("warehouse_id") is not None:
            if int(row.get("warehouse_id")) != int(warehouse_id):
                continue
        try:
            ts = int(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if now_ts is not None and ts < int(now_ts) - int(history_days) * 86400:
            continue
        try:
            nodes = row.get("nodes_json", row.get("nodes", []))
            if isinstance(nodes, str):
                nodes = json.loads(nodes)
        except (TypeError, ValueError):
            continue
        if not isinstance(nodes, list):
            continue
        uid = str(row.get("uid") or "")
        for node in nodes:
            parsed = _node_reading(node, ts)
            if parsed is None:
                continue
            addr, sample = parsed
            filtered.append((uid, addr, sample))
    filtered.sort(key=lambda item: item[2]["ts"])

    identities = set()
    buckets = {}
    for uid, addr, sample in filtered:
        identity = sample.get("identity")
        if identity is not None:
            key = (uid, addr, identity[0], identity[1])
            if key in identities:
                continue
            identities.add(key)
        bucket = sample["ts"] // BIN_SECONDS
        key = (uid, addr, bucket)
        previous = buckets.get(key)
        if previous is None or sample["ts"] > previous["ts"]:
            buckets[key] = sample

    series = {}
    for (uid, addr, bucket), sample in buckets.items():
        series.setdefault(uid, {}).setdefault(addr, {})[bucket] = sample

    weather = _parse_weather(weather_rows)
    horizon_bins = [hour * 3600 // BIN_SECONDS for hour in HORIZONS_HOURS]
    result = []
    for uid, by_addr in series.items():
        all_buckets = sorted({bucket for by_bucket in by_addr.values()
                              for bucket in by_bucket})
        for addr, by_bucket in by_addr.items():
            for bucket in all_buckets:
                current = by_bucket.get(bucket)
                if current is None:
                    continue
                previous = by_bucket.get(bucket - 3600 // BIN_SECONDS)
                target_rows = [by_bucket.get(bucket + offset)
                               for offset in horizon_bins]
                if any(target is None for target in target_rows):
                    continue
                timestamp = bucket * BIN_SECONDS + BIN_SECONDS // 2
                current_air = {"ts": timestamp, "temp": current["temp"],
                               "rh": current["rh"]}
                previous_air = ({"ts": (bucket - 6) * BIN_SECONDS + BIN_SECONDS // 2,
                                 "temp": previous["temp"], "rh": previous["rh"]}
                                if previous else None)
                peers = []
                for peer_addr, peer_buckets in by_addr.items():
                    if peer_addr == addr:
                        continue
                    peer = peer_buckets.get(bucket)
                    if peer:
                        peers.append({"ts": timestamp, "temp": peer["temp"],
                                      "rh": peer["rh"]})
                outside = _nearest_weather(weather, timestamp)
                features = build_feature_vector(current_air, previous_air,
                                                peers, outside)
                current_vapor = (current["rh"] *
                                 saturation_vapor_pressure_hpa(current["temp"]) / 100.0)
                target_values = []
                for target in target_rows:
                    vapor = (target["rh"] *
                             saturation_vapor_pressure_hpa(target["temp"]) / 100.0)
                    target_values.extend((target["temp"] - current["temp"],
                                          vapor - current_vapor))
                result.append({
                    "ts": timestamp,
                    "uid": uid,
                    "addr": addr,
                    "features": features,
                    "current": [current["temp"], current_vapor],
                    "targets": target_values,
                })
    result.sort(key=lambda row: (row["ts"], row["uid"], row["addr"]))
    return result


def load_training_rows(conn, warehouse_id, *, now_ts=None,
                       history_days=TRAINING_HISTORY_DAYS):
    """Load a warehouse's raw records and make training examples."""
    now_ts = int(now_ts or time.time())
    since = now_ts - int(history_days) * 86400
    snapshots = conn.execute(
        "SELECT uid,warehouse_id,ts,nodes_json FROM snapshots "
        "WHERE warehouse_id=? AND ts>=? ORDER BY uid,ts,id",
        (int(warehouse_id), since)).fetchall()
    weather = conn.execute(
        "SELECT weather_ts,temperature_c,rh_pct FROM warehouse_weather_observations "
        "WHERE warehouse_id=? AND weather_ts>=? ORDER BY weather_ts",
        (int(warehouse_id), since)).fetchall()
    return build_training_rows(
        [{"uid": row[0], "warehouse_id": row[1], "ts": row[2],
          "nodes_json": row[3]} for row in snapshots],
        [{"ts": row[0], "temperature_c": row[1], "rh_pct": row[2]}
         for row in weather], warehouse_id=warehouse_id,
        now_ts=now_ts, history_days=history_days)


def load_probe_histories(conn, uid, *, now_ts=None, history_hours=6,
                         warehouse_id=None):
    """Read deduplicated recent sensor values for station-side inference."""
    now_ts = int(now_ts or time.time())
    since = now_ts - int(history_hours) * 3600
    if warehouse_id is None:
        rows = conn.execute(
            "SELECT ts,nodes_json FROM snapshots WHERE uid=? AND ts>=? "
            "ORDER BY ts,id", (str(uid), since)).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts,nodes_json FROM snapshots WHERE uid=? AND warehouse_id=? "
            "AND ts>=? ORDER BY ts,id",
            (str(uid), int(warehouse_id), since)).fetchall()
    by_addr = {}
    for snapshot_ts, encoded in rows:
        try:
            nodes = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        for node in nodes if isinstance(nodes, list) else []:
            parsed = _node_reading(node, int(snapshot_ts))
            if parsed is None:
                continue
            addr, sample = parsed
            if sample["ts"] > now_ts:
                continue
            bucket = sample["ts"] // BIN_SECONDS
            existing = by_addr.setdefault(addr, {}).get(bucket)
            if existing is None or sample["ts"] > existing["ts"]:
                by_addr[addr][bucket] = sample
    return {addr: sorted(rows.values(), key=lambda item: item["ts"])
            for addr, rows in by_addr.items()}


def load_current_weather(conn, warehouse_id, now_ts=None):
    """Get a fresh weather context for inference without using future actuals."""
    now_ts = int(now_ts or time.time())
    location = conn.execute(
        "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
        (int(warehouse_id),)).fetchone()
    if location:
        lat_key, lon_key = f"{float(location[0]):.6f}", f"{float(location[1]):.6f}"
        run = conn.execute(
            "SELECT current_json,fetched_ts FROM warehouse_weather_runs "
            "WHERE warehouse_id=? AND lat_key=? AND lon_key=? "
            "ORDER BY fetched_ts DESC,id DESC LIMIT 1",
            (int(warehouse_id), lat_key, lon_key)).fetchone()
        if run and 0 <= now_ts - int(run[1]) <= 3 * 3600:
            try:
                payload = json.loads(run[0])
                current = payload.get("current", payload)
                if isinstance(current, dict):
                    weather_ts = int(current.get("ts", run[1]))
                    temp = _number(current.get(
                        "temperature_c", current.get("temperature_2m",
                                                     current.get("temp_c"))))
                    rh = _number(current.get(
                        "rh_pct", current.get("relative_humidity_2m",
                                              current.get("relative_humidity_pct"))))
                    if (temp is not None and rh is not None and
                            0 <= now_ts - weather_ts <= 3 * 3600):
                        return {"ts": weather_ts, "temperature_c": temp,
                                "rh_pct": rh}
            except (TypeError, ValueError):
                pass
        row = conn.execute(
            "SELECT weather_ts,temperature_c,rh_pct FROM warehouse_weather_observations "
            "WHERE warehouse_id=? AND lat_key=? AND lon_key=? AND weather_ts<=? "
            "ORDER BY weather_ts DESC LIMIT 1",
            (int(warehouse_id), lat_key, lon_key, now_ts)).fetchone()
        if row and 0 <= now_ts - int(row[0]) <= 3 * 3600:
            return {"ts": row[0], "temperature_c": row[1], "rh_pct": row[2]}
    return None
