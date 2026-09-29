# -*- coding: utf-8 -*-
"""
server.py - 粮仓温湿度监测系统 中心站（独立部署，零依赖）
================================================================
只依赖 Python 标准库（3.8+），无需 pip install。

启动：  python server.py [端口=8000]
访问：  http://127.0.0.1:8000        （3D 总览页）
        http://127.0.0.1:8000/pole.html?uid=XXXX     （单杆详情页）

杆子上报入口：POST /api/v1/snapshot（与固件 net.cpp 对接，接口见下方 API 表）

目录结构（本文件夹可整体拷贝到任何电脑独立部署）：
    station/
    ├── server.py        本文件（后端 + 静态服务）
    ├── station.db       SQLite（自动创建：poles/snapshots/alert_log/config/env）
    ├── web/             前端页面（可交给设计师重排版）
    │   ├── index.html        3D 部署总览
    │   ├── pole.html         单杆详情（曲线/告警/控制）
    │   └── assets/css|js|vendor  样式 / 脚本 / 本地化第三方库
    │                             （three.js r128 + echarts 5.4.3，
    │                              已本地化，网页完全离线可用，不依赖外网）

API：
    POST /api/v1/snapshot              杆子上报（识别建档/存库/告警/回指令，支持 bat_mv）
    GET  /api/v1/poles                 杆列表（含最新快照、节点级在线状态、电量）
    GET  /api/v1/poles/{uid}           单杆详情（档案+最新快照）
    GET  /api/v1/poles/{uid}/data?hours=24|168|720   历史数据（曲线用）
    POST /api/v1/poles/{uid}/cmd       下发远程指令 {"cmd":"mode=debug"}
    POST /api/v1/poles/{uid}/config    修改档案（名称/坐标/间距/阈值）
    GET  /api/v1/alerts                告警历史/活动告警
    GET  /api/v1/config                全局配置（仓库尺寸/粮重/密度/网格步长）
    POST /api/v1/config                修改全局配置（白名单键，保存本地下次启动生效）
    POST /api/v1/env                   环境监测上报 {"temp":..,"rh":..,"co2":..,...}
    GET  /api/v1/env/latest            最新一条环境数据
    GET  /api/v1/env?hours=24          环境历史（曲线用）
    GET  /api/v1/heatmap?step=0.5      粮堆三维热场插值（IDW，相邻杆节点插值）
    GET  /api/v1/forecast?pile=1&lat=..&lon=..  在线测点的长历史温湿度趋势 + 天气旁证
    GET  /api/v1/probe-forecast?uid=..&addr=1  单节点 S3 预测 + Station 独立重算
    GET  /api/v1/actuators             风机/灯光/通风只读能力占位（当前不可控）
    POST /api/v1/crop-emc             来源范围内的作物平衡含水率参考计算
    GET/POST /api/v1/crop-profile     按粮堆读取/保存作物模型选项
    GET  /api/ping                     连通性检查

识别机制：首次收到 pole_uid 自动建档（默认名"探杆 <uid>"），
坐标/名称只在中心站配置，杆子固件不感知。
节点记忆：曾上报过的节点地址永久记忆；突然失联时 node_states 返回 online=0，
前端 3D 变灰显示，便于拔杆排查。
"""
import json
import csv
import io
import math
import os
import sqlite3
import sys
import time
import threading
import webbrowser
import zipfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote
from forecast_model import (HISTORY_MULTIPLIER, MIN_SAMPLES,
                            SAMPLE_PERIOD_SECONDS, predict_air_state)
from crop_emc_model import calculate_emc
from weather_assisted_forecast import predict_weather_assisted_air_state
from weather_cadence_policy import (advance_weather_cadence_state,
                                    effective_normal_interval)
from runtime_paths import resolve_runtime_paths

# Windows 控制台默认 GBK，直接 print 中文会 UnicodeEncodeError 崩溃（曾发生）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_RUNTIME_PATHS = resolve_runtime_paths(
    __file__,
    frozen=bool(getattr(sys, "frozen", False)),
    local_app_data=os.environ.get("LOCALAPPDATA"),
    user_home=os.path.expanduser("~"),
    db_override=os.environ.get("GRAINSILO_DB_PATH"),
)
ROOT = _RUNTIME_PATHS["root"]
WEB = _RUNTIME_PATHS["web"]
DATA_ROOT = _RUNTIME_PATHS["data"]
DB = _RUNTIME_PATHS["db"]
LOG_ROOT = _RUNTIME_PATHS["logs"]

OFFLINE_SEC = 45 * 60          # 3 个上报周期无快照 -> 杆离线
ALARM_TRIGGER_CNT = 2          # 连续超限次数才触发（抑制毛刺，与固件一致）
ALARM_RECOVER_CNT = 2          # 连续正常次数才恢复
MAX_REQUEST_BODY_BYTES = 1024 * 1024  # LAN API 请求体上限，避免无界内存读取

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".png": "image/png",
        ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

DEFAULT_TH = {"temp_high": "30.0", "temp_low": "-5.0", "rh_high": "70.0", "rh_low": "0.0"}

# 全局配置（仓库/粮堆/热场），全部存 SQLite config 表，本地持久化
DEFAULT_GLOBAL = {
    "wh_l": "20.0",            # 仓库长（米）
    "wh_w": "10.0",            # 仓库宽（米）
    "wh_h": "8.0",             # 仓库高（米）
    "grain_weight": "0.0",     # 当前粮食重量（kg），>0 时 3D 显示粮堆
    "grain_density": "750.0",  # 粮食容重（kg/m³），小麦典型 750
    "heatmap_step": "0.5",     # 热场插值网格步长（米）
    "node_offline_sec": "2700",# 节点失联判定秒数（45min）
    "normal_interval_sec": "60", # 自适应正常档采样/上报周期
    "fast_interval_sec": "10",   # 风险档采样/上报周期（与采样周期联动）
}
CONFIG_KEYS = list(DEFAULT_TH) + list(DEFAULT_GLOBAL)
SCHEMA_VERSION = 9
THRESHOLD_KEYS = ("temp_high", "temp_low", "rh_high", "rh_low")
INTERVAL_KEYS = ("normal_interval_sec", "fast_interval_sec")
DEVICE_SETTING_KEYS = THRESHOLD_KEYS + INTERVAL_KEYS
WEATHER_CADENCE_EVALUATION_INTERVAL = 300
WEATHER_CADENCE_CACHE_MAX_AGE = 2 * 60 * 60
DEFAULT_ENV_REGISTRY = (
    ("temp", "仓内温度", "°C", "1", "station"),
    ("rh", "仓内湿度", "%RH", "1", "station"),
    ("co2", "二氧化碳", "ppm", "1", "station"),
    ("lux", "光照", "lx", "1", "station"),
    ("ventilation", "通风状态", "state", "1", "manual"),
)
ACTUATOR_CAPABILITIES = (
    ("fan", "风机"),
    ("lighting", "灯光"),
    ("ventilation", "通风设备"),
)

# 节点级在线跟踪（内存 + pole_nodes 表持久化，重启不丢记忆）
_node_last = {}      # (uid, addr) -> 最后上报 ts（记忆：曾见过的节点永久保留）
_node_seen = {}      # uid -> {addr: True}


def remember_nodes(conn, uid, nodes, ts):
    """记录节点踪迹：内存 + 落库（记忆状态，重启后仍记得每杆的节点数）"""
    seen = _node_seen.setdefault(uid, {})
    for nd in nodes:
        addr = nd.get("addr")
        if addr is None:
            continue
        seen[addr] = True
        _node_last[(uid, addr)] = ts
        conn.execute("INSERT OR REPLACE INTO pole_nodes(uid,addr,last_ts) "
                     "VALUES(?,?,?)", (uid, addr, ts))


def load_node_memory():
    """启动时载入节点记忆；pole_nodes 为空（旧库升级）则从快照历史补齐"""
    conn = db()
    rows = conn.execute("SELECT uid,addr,last_ts FROM pole_nodes").fetchall()
    if not rows:
        latest_ts = dict(conn.execute(
            "SELECT uid, MAX(ts) FROM snapshots GROUP BY uid").fetchall())
        for uid, nodes_json in conn.execute("SELECT uid,nodes_json FROM snapshots"):
            for nd in json.loads(nodes_json):
                addr = nd.get("addr")
                if addr is None:
                    continue
                _node_seen.setdefault(uid, {})[addr] = True
                _node_last[(uid, addr)] = latest_ts.get(uid, 0)
        print("[STATION] 节点记忆已从历史快照补齐")
    else:
        for uid, addr, last_ts in rows:
            _node_seen.setdefault(uid, {})[addr] = True
            _node_last[(uid, addr)] = last_ts
    conn.close()


def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS poles(
        uid TEXT PRIMARY KEY, name TEXT, x REAL DEFAULT 0, y REAL DEFAULT 0,
        z REAL DEFAULT 0, spacing_m REAL DEFAULT 0.5, pending_cmd TEXT DEFAULT '',
        last_ts INT DEFAULT 0, status INT DEFAULT 1, bat_mv REAL DEFAULT 0,
        temp_high REAL DEFAULT NULL, temp_low REAL DEFAULT NULL,
        rh_high REAL DEFAULT NULL, rh_low REAL DEFAULT NULL,
        normal_interval_sec INT DEFAULT NULL,
        fast_interval_sec INT DEFAULT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, ts INT,
        mode INT, alarm INT, nodes_json TEXT, bat_mv REAL DEFAULT 0,
        reporting_json TEXT DEFAULT '{}')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alert_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, node_addr INT, ch TEXT,
        value REAL, th REAL, begin_ts INT, end_ts INT DEFAULT 0, active INT DEFAULT 1)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS forecast_alert_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,
        node_addr INT NOT NULL, ch TEXT NOT NULL, forecast_hour INT NOT NULL,
        value REAL NOT NULL, th REAL NOT NULL, begin_ts INT NOT NULL,
        last_ts INT NOT NULL, end_ts INT DEFAULT 0, active INT DEFAULT 1)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_alert_active "
                 "ON forecast_alert_log(uid,node_addr,ch,active)")
    conn.execute("""CREATE TABLE IF NOT EXISTS config(
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS env(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT, data_json TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_location(
        id INTEGER PRIMARY KEY CHECK(id=1), lat REAL NOT NULL, lon REAL NOT NULL,
        updated_ts INT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, lat_key TEXT NOT NULL,
        lon_key TEXT NOT NULL, run_slot INT NOT NULL, fetched_ts INT NOT NULL,
        current_json TEXT NOT NULL, series_json TEXT NOT NULL,
        source TEXT NOT NULL, UNIQUE(lat_key,lon_key,run_slot))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pole_nodes(
        uid TEXT, addr INT, last_ts INT, PRIMARY KEY(uid, addr))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS node_positions(
        uid TEXT NOT NULL, addr INT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
        z REAL NOT NULL, calibrated_ts INT NOT NULL,
        PRIMARY KEY(uid, addr))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS piles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, weight_kg REAL DEFAULT 0, density REAL DEFAULT 750,
        x REAL DEFAULT 10, z REAL DEFAULT 5, l REAL DEFAULT 20, w REAL DEFAULT 10,
        created_ts INT, updated_ts INT,
        temp_high REAL DEFAULT NULL, temp_low REAL DEFAULT NULL,
        rh_high REAL DEFAULT NULL, rh_low REAL DEFAULT NULL,
        normal_interval_sec INT DEFAULT NULL,
        fast_interval_sec INT DEFAULT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS device_settings(
        uid TEXT PRIMARY KEY, revision INT NOT NULL, applied_revision INT DEFAULT 0,
        temp_high_centi INT NOT NULL, temp_low_centi INT NOT NULL,
        rh_high_centi INT NOT NULL, rh_low_centi INT NOT NULL,
        normal_interval_sec INT NOT NULL, fast_interval_sec INT NOT NULL,
        updated_ts INT NOT NULL, applied_ts INT DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS crop_profiles(
        pile_id INTEGER PRIMARY KEY,
        crop_type TEXT NOT NULL CHECK(crop_type IN ('paddy','wheat','corn')),
        wheat_type TEXT NOT NULL DEFAULT 'soft' CHECK(wheat_type IN ('soft','hard')),
        paddy_path TEXT NOT NULL DEFAULT 'both'
            CHECK(paddy_path IN ('both','adsorption','desorption')),
        updated_ts INT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trash(
        uid TEXT PRIMARY KEY, name TEXT, pile_id INT, pile_name TEXT,
        snap_count INT DEFAULT 0, bat_mv REAL DEFAULT 0, deleted_ts INT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_meta(
        key TEXT PRIMARY KEY, value TEXT NOT NULL, applied_ts INT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT NOT NULL,
        actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
        entity_id TEXT DEFAULT '', detail_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS environment_registry(
        key TEXT PRIMARY KEY, label TEXT NOT NULL, unit TEXT DEFAULT '',
        enabled INT DEFAULT 1, source TEXT DEFAULT 'station', updated_ts INT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS config_revisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT NOT NULL,
        actor TEXT NOT NULL, detail_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS warehouses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        shape_type TEXT NOT NULL DEFAULT 'flat'
            CHECK(shape_type IN ('flat','silo')),
        length_m REAL NOT NULL DEFAULT 20,
        width_m REAL NOT NULL DEFAULT 10,
        height_m REAL NOT NULL DEFAULT 8,
        diameter_m REAL NOT NULL DEFAULT 12,
        repose_angle_deg REAL NOT NULL DEFAULT 30,
        active INT NOT NULL DEFAULT 1,
        created_ts INT NOT NULL)""")
    try:
        conn.execute("ALTER TABLE warehouses ADD COLUMN repose_angle_deg "
                     "REAL NOT NULL DEFAULT 30")
    except sqlite3.OperationalError:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS warehouse_config(
        warehouse_id INT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY(warehouse_id,key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS warehouse_weather(
        warehouse_id INT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL,
        updated_ts INT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS warehouse_weather_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, warehouse_id INT NOT NULL,
        lat_key TEXT NOT NULL, lon_key TEXT NOT NULL, run_slot INT NOT NULL,
        fetched_ts INT NOT NULL, current_json TEXT NOT NULL,
        series_json TEXT NOT NULL, source TEXT NOT NULL,
        UNIQUE(warehouse_id,lat_key,lon_key,run_slot))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS warehouse_weather_observations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, warehouse_id INT NOT NULL,
        lat_key TEXT NOT NULL, lon_key TEXT NOT NULL, weather_ts INT NOT NULL,
        temperature_c REAL, rh_pct REAL, dew_point_c REAL,
        source TEXT NOT NULL, fetched_ts INT NOT NULL,
        UNIQUE(warehouse_id,lat_key,lon_key,weather_ts))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_observations_lookup "
                 "ON warehouse_weather_observations(warehouse_id,weather_ts)")
    for k, v in dict(DEFAULT_TH, **DEFAULT_GLOBAL).items():
        conn.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)", (k, v))
    now = int(time.time())
    legacy_dims = {key: float(value) for key, value in conn.execute(
        "SELECT key,value FROM config WHERE key IN ('wh_l','wh_w','wh_h')")}
    conn.execute("""INSERT OR IGNORE INTO warehouses
        (id,name,shape_type,length_m,width_m,height_m,diameter_m,active,created_ts)
        VALUES(1,'1号平房仓（主仓）','flat',?,?,?,?,1,?)""",
        (legacy_dims.get('wh_l', 20.0), legacy_dims.get('wh_w', 10.0),
         legacy_dims.get('wh_h', 8.0), max(legacy_dims.get('wh_l', 20.0),
                                             legacy_dims.get('wh_w', 10.0)), now))
    conn.execute("INSERT OR IGNORE INTO warehouse_config(warehouse_id,key,value) "
                 "SELECT 1,key,value FROM config")
    for table in ("poles", "snapshots", "alert_log", "forecast_alert_log",
                  "env", "weather_runs", "piles", "trash", "audit_log",
                  "config_revisions"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN warehouse_id INT NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_warehouse "
                 "ON snapshots(warehouse_id,ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_warehouse "
                 "ON alert_log(warehouse_id,active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_alert_warehouse "
                 "ON forecast_alert_log(warehouse_id,active)")
    try:
        legacy_weather = conn.execute(
            "SELECT lat,lon,updated_ts FROM weather_location WHERE id=1").fetchone()
        if legacy_weather:
            conn.execute("INSERT OR IGNORE INTO warehouse_weather"
                         "(warehouse_id,lat,lon,updated_ts) VALUES(1,?,?,?)",
                         legacy_weather)
    except sqlite3.OperationalError:
        pass
    conn.execute("INSERT OR IGNORE INTO warehouse_weather_runs"
                 "(warehouse_id,lat_key,lon_key,run_slot,fetched_ts,current_json,"
                 "series_json,source) SELECT warehouse_id,lat_key,lon_key,run_slot,"
                 "fetched_ts,current_json,series_json,source FROM weather_runs")
    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value,applied_ts) VALUES(?,?,?)",
                 ("schema_version", str(SCHEMA_VERSION), now))
    for key, label, unit, enabled, source in DEFAULT_ENV_REGISTRY:
        conn.execute("INSERT OR IGNORE INTO environment_registry"
                     "(key,label,unit,enabled,source,updated_ts) VALUES(?,?,?,?,?,?)",
                     (key, label, unit, int(enabled), source, now))
    # 旧库迁移：补 bat_mv 列
    for tbl in ("poles", "snapshots"):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN bat_mv REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE snapshots ADD COLUMN reporting_json TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    # 旧库迁移：poles 补 pile_id（粮堆绑定）
    try:
        conn.execute("ALTER TABLE poles ADD COLUMN pile_id INT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    # 旧库迁移：poles 补 deleted（回收箱软删除标记，0=正常 1=已删入回收箱）
    try:
        conn.execute("ALTER TABLE poles ADD COLUMN deleted INT DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_poles_warehouse "
                 "ON poles(warehouse_id,deleted)")
    # Threshold overrides are nullable so older records inherit the next scope.
    for tbl in ("piles", "poles"):
        for key in DEVICE_SETTING_KEYS:
            try:
                column_type = "INT" if key in INTERVAL_KEYS else "REAL"
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {key} "
                             f"{column_type} DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
    # 旧库迁移：旧的全局粮重配置 -> 默认粮堆，并绑定全部探杆
    cnt = conn.execute("SELECT COUNT(*) FROM piles").fetchone()[0]
    if cnt == 0:
        r = conn.execute("SELECT value FROM config WHERE key='grain_weight'").fetchone()
        if r and float(r[0]) > 0:
            g = {k: float(v) for k, v in conn.execute(
                "SELECT key,value FROM config WHERE key IN ('wh_l','wh_w','grain_density')")}
            now = int(time.time())
            cur = conn.execute(
                "INSERT INTO piles(name,weight_kg,density,x,z,l,w,created_ts,updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("1号粮堆", float(r[0]), g.get("grain_density", 750),
                 g.get("wh_l", 20) / 2, g.get("wh_w", 10) / 2,
                 g.get("wh_l", 20), g.get("wh_w", 10), now, now))
            conn.execute("UPDATE poles SET pile_id=? WHERE deleted=0 OR deleted IS NULL",
                         (cur.lastrowid,))
            print(f"[MIGRATE] 旧全局粮重 -> 默认粮堆 id={cur.lastrowid}，探杆已绑定")
    conn.commit()
    conn.close()
    load_node_memory()


def db():
    return sqlite3.connect(DB)


def _snapshot_time():
    """Return the Station receive time, independent of device clock settings."""
    return int(time.time())


def _store_minute_snapshot(conn, uid, snap, received_ts):
    """Keep one rolling latest-value snapshot per pole and minute.

    S3 may POST more frequently for live display and control-loop diagnostics;
    the durable history retains the latest accepted values in that minute.
    """
    minute_ts = int(received_ts) - (int(received_ts) % 60)
    nodes_json = json.dumps(snap.get("nodes", []), ensure_ascii=False)
    battery_mv = float(snap.get("bat_mv") or 0)
    reporting = {key: snap[key] for key in (
        "sample_interval_ms", "report_interval_ms", "adaptive_fast",
        "measurement_risk", "prediction_risk", "report_queue_replacements")
        if key in snap}
    reporting_json = json.dumps(reporting, ensure_ascii=False, sort_keys=True)
    warehouse_id = warehouse_for_uid(conn, uid)
    row = conn.execute(
        "SELECT id FROM snapshots WHERE uid=? AND warehouse_id=? AND ts>=? AND ts<? "
        "ORDER BY id DESC LIMIT 1",
        (uid, warehouse_id, minute_ts, minute_ts + 60)).fetchone()
    values = (snap.get("mode", 0), snap.get("alarm", 0), nodes_json,
              battery_mv, reporting_json)
    if row:
        conn.execute(
            "UPDATE snapshots SET mode=?,alarm=?,nodes_json=?,bat_mv=?,reporting_json=? "
            "WHERE id=?", values + (row[0],))
        return row[0]
    cur = conn.execute(
        "INSERT INTO snapshots(uid,ts,mode,alarm,nodes_json,bat_mv,reporting_json,"
        "warehouse_id) VALUES(?,?,?,?,?,?,?,?)",
        (uid, minute_ts) + values + (warehouse_id,))
    return cur.lastrowid


def write_audit(conn, action, entity, entity_id="", detail=None, actor="station-api",
                warehouse_id=1):
    """Append an auditable business change without storing secrets."""
    payload = detail if isinstance(detail, dict) else {}
    conn.execute("INSERT INTO audit_log(ts,actor,action,entity,entity_id,detail_json,"
                 "warehouse_id) VALUES(?,?,?,?,?,?,?)",
                 (int(time.time()), actor, action, entity, str(entity_id),
                  json.dumps(payload, ensure_ascii=False, sort_keys=True), warehouse_id))


def get_cfg(conn, warehouse_id=1):
    values = dict(conn.execute("SELECT key,value FROM config"))
    try:
        values.update(conn.execute(
            "SELECT key,value FROM warehouse_config WHERE warehouse_id=?",
            (int(warehouse_id),)).fetchall())
    except sqlite3.OperationalError:
        pass
    return {key: float(value) for key, value in values.items()}


def warehouse_for_uid(conn, uid):
    row = conn.execute("SELECT warehouse_id FROM poles WHERE uid=?", (uid,)).fetchone()
    return int(row[0]) if row else 1


def _validate_thresholds(values):
    """Validate one effective threshold set before it can drive alarm state."""
    temp_low, temp_high = float(values["temp_low"]), float(values["temp_high"])
    rh_low, rh_high = float(values["rh_low"]), float(values["rh_high"])
    if not (-80.0 <= temp_low < temp_high <= 100.0):
        raise ValueError("温度阈值必须满足 -80 ≤ 低温 < 高温 ≤ 100°C")
    if not (0.0 <= rh_low < rh_high <= 100.0):
        raise ValueError("湿度阈值必须满足 0 ≤ 低湿 < 高湿 ≤ 100%RH")


def _threshold_updates(body, allow_clear=True):
    updates = {}
    for key in THRESHOLD_KEYS:
        if key not in body:
            continue
        raw = body[key]
        if raw is None and allow_clear:
            updates[key] = None
            continue
        if isinstance(raw, bool):
            raise ValueError(f"{key} 必须为数值")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须为数值")
        if not math.isfinite(value):
            raise ValueError(f"{key} 必须为有限数值")
        if key.startswith("temp_") and not -80.0 <= value <= 100.0:
            raise ValueError("温度阈值范围为 -80 至 100°C")
        if key.startswith("rh_") and not 0.0 <= value <= 100.0:
            raise ValueError("湿度阈值范围为 0 至 100%RH")
        updates[key] = value
    return updates


def _interval_updates(body, allow_clear=True):
    updates = {}
    for key in INTERVAL_KEYS:
        if key not in body:
            continue
        raw = body[key]
        if raw is None and allow_clear:
            updates[key] = None
            continue
        if isinstance(raw, bool):
            raise ValueError(f"{key} 必须为整数秒")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须为整数秒")
        if str(raw).strip() not in (str(value), f"{value}.0"):
            raise ValueError(f"{key} 必须为整数秒")
        if key == "normal_interval_sec" and not 1 <= value <= 86400:
            raise ValueError("常规采样/上报周期范围为 1 至 86400 秒")
        if key == "fast_interval_sec" and not 5 <= value <= 3600:
            raise ValueError("风险采样/上报周期范围为 5 至 3600 秒")
        updates[key] = value
    return updates


def _validate_intervals(values):
    normal = int(values["normal_interval_sec"])
    fast = int(values["fast_interval_sec"])
    if not (1 <= normal <= 86400 and 5 <= fast <= 3600 and fast <= normal):
        raise ValueError("周期必须满足：风险档 5–3600 秒，常规档 1–86400 秒，且风险档不大于常规档")


def effective_intervals(conn, uid=None, pile_id=None, warehouse_id=None):
    """Resolve adaptive normal/risk periods: warehouse -> pile -> probe."""
    if uid:
        warehouse_id = warehouse_for_uid(conn, uid)
    elif pile_id is not None and warehouse_id is None:
        row = conn.execute("SELECT warehouse_id FROM piles WHERE id=?",
                           (pile_id,)).fetchone()
        warehouse_id = row[0] if row else 1
    cfg = get_cfg(conn, warehouse_id or 1)
    values = {key: int(cfg[key]) for key in INTERVAL_KEYS}
    sources = {key: "warehouse" for key in INTERVAL_KEYS}
    probe_overrides = None
    if uid:
        row = conn.execute(
            "SELECT pile_id," + ",".join(INTERVAL_KEYS) +
            " FROM poles WHERE uid=? AND (deleted=0 OR deleted IS NULL)",
            (uid,)).fetchone()
        if row:
            if pile_id is None:
                pile_id = row[0]
            probe_overrides = dict(zip(INTERVAL_KEYS, row[1:]))
    if pile_id is not None:
        row = conn.execute("SELECT " + ",".join(INTERVAL_KEYS) +
                           " FROM piles WHERE id=?", (pile_id,)).fetchone()
        if row:
            for key, raw in zip(INTERVAL_KEYS, row):
                if raw is not None:
                    values[key], sources[key] = int(raw), "pile"
    if probe_overrides:
        for key, raw in probe_overrides.items():
            if raw is not None:
                values[key], sources[key] = int(raw), "probe"
    return {"values": values, "sources": sources}


def effective_device_settings(conn, uid=None, pile_id=None, warehouse_id=None):
    thresholds = effective_thresholds(conn, uid=uid, pile_id=pile_id,
                                      warehouse_id=warehouse_id)
    intervals = effective_intervals(conn, uid=uid, pile_id=pile_id,
                                    warehouse_id=warehouse_id)
    return {"values": {**thresholds["values"], **intervals["values"]},
            "sources": {**thresholds["sources"], **intervals["sources"]}}


def _weather_cadence_state(conn, uid, warehouse_id=None):
    warehouse_id = int(warehouse_id or warehouse_for_uid(conn, uid))
    row = conn.execute("SELECT detail_json FROM audit_log WHERE action=? "
                       "AND entity='probe' AND entity_id=? AND warehouse_id=? "
                       "ORDER BY id DESC LIMIT 1",
                       ("weather_cadence.evaluate", str(uid), warehouse_id)).fetchone()
    if not row:
        return {"mode": "normal", "safe_reports": 0,
                "status": "waiting_for_validated_forecast", "evaluated_ts": 0}
    try:
        state = json.loads(row[0])
    except (TypeError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {
        "mode": "normal", "safe_reports": 0,
        "status": "waiting_for_validated_forecast", "evaluated_ts": 0}


def _weather_cadence_predictions(conn, uid, warehouse_id, now_ts):
    """Build forecasts from cached weather only; never block S3 ingest on network IO."""
    location = conn.execute(
        "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
        (int(warehouse_id),)).fetchone()
    addresses = [int(row[0]) for row in conn.execute(
        "SELECT addr FROM pole_nodes WHERE uid=? ORDER BY addr", (uid,)).fetchall()
        if row[0] is not None]
    if not addresses:
        return [], {"expected_nodes": 0, "ready_nodes": 0,
                    "weather_source": None, "weather_fetched_ts": None}
    if not location:
        return [{"status": "not_configured", "forecast": []}
                for _ in addresses], {"expected_nodes": len(addresses),
                    "ready_nodes": 0, "weather_source": None,
                    "weather_fetched_ts": None}

    lat, lon = float(location[0]), float(location[1])
    cached_weather = _cached_weather_run(
        conn, lat, lon, now_ts,
        max_age_sec=WEATHER_CADENCE_CACHE_MAX_AGE,
        warehouse_id=int(warehouse_id))
    if not cached_weather or cached_weather.get("status") != "ready":
        return [{"status": "weather_unavailable", "forecast": []}
                for _ in addresses], {"expected_nodes": len(addresses),
                    "ready_nodes": 0, "weather_source": None,
                    "weather_fetched_ts": None}

    history_rows = conn.execute("""SELECT weather_ts,temperature_c,rh_pct
        FROM warehouse_weather_observations
        WHERE warehouse_id=? AND lat_key=? AND lon_key=? AND weather_ts>=?
        ORDER BY weather_ts DESC LIMIT 1080""",
        (int(warehouse_id), f"{lat:.6f}", f"{lon:.6f}",
         int(now_ts) - 45 * 86400)).fetchall()
    weather_history = [{"ts": row[0], "temperature_c": row[1], "rh_pct": row[2]}
                       for row in reversed(history_rows)]
    weather_forecast = cached_weather.get("series") or []
    predictions = []
    for addr in addresses:
        samples = _probe_hourly_samples(conn, uid, addr, int(warehouse_id))
        predictions.append(predict_weather_assisted_air_state(
            samples, weather_history, weather_forecast, int(now_ts)))
    return predictions, {"expected_nodes": len(addresses),
                         "ready_nodes": sum(item.get("status") == "ready"
                                             for item in predictions),
                         "weather_source": cached_weather.get("source"),
                         "weather_fetched_ts": cached_weather.get("fetched_ts")}


def _evaluate_weather_cadence(conn, uid, warehouse_id, now_ts):
    """Update persistent prediction policy at a bounded rate per S3 report."""
    previous = _weather_cadence_state(conn, uid, warehouse_id)
    try:
        evaluated_ts = int(previous.get("evaluated_ts", 0))
    except (TypeError, ValueError, OverflowError):
        evaluated_ts = 0
    if evaluated_ts and int(now_ts) - evaluated_ts < WEATHER_CADENCE_EVALUATION_INTERVAL:
        return previous

    predictions, metadata = _weather_cadence_predictions(
        conn, uid, warehouse_id, int(now_ts))
    thresholds = effective_thresholds(
        conn, uid=uid, warehouse_id=warehouse_id)["values"]
    updated = advance_weather_cadence_state(
        previous, predictions, thresholds, now_ts=int(now_ts))
    updated.update(metadata)
    updated["evaluated_ts"] = int(now_ts)
    updated["policy"] = "validated_weather_forecast_v1"
    addresses = [int(row[0]) for row in conn.execute(
        "SELECT addr FROM pole_nodes WHERE uid=? ORDER BY addr", (uid,)).fetchall()
        if row[0] is not None]
    _sync_weather_forecast_alerts(
        conn, uid, warehouse_id, addresses, predictions, thresholds, int(now_ts))
    write_audit(conn, "weather_cadence.evaluate", "probe", uid, updated,
                actor="weather-policy", warehouse_id=warehouse_id)
    return updated


def _device_setting_targets(conn, uid, resolved=None):
    resolved = resolved or effective_device_settings(conn, uid=uid)
    values = dict(resolved["values"])
    sources = dict(resolved["sources"])
    warehouse_id = warehouse_for_uid(conn, uid)
    policy = _weather_cadence_state(conn, uid, warehouse_id)
    base_normal = int(values["normal_interval_sec"])
    effective_normal = effective_normal_interval(
        policy.get("mode"), base_normal, values["fast_interval_sec"])
    values["normal_interval_sec"] = effective_normal
    if effective_normal != base_normal:
        sources["normal_interval_sec"] = "weather_forecast"
    policy = dict(policy)
    policy["base_normal_interval_sec"] = base_normal
    policy["effective_normal_interval_sec"] = effective_normal
    return {"values": values, "sources": sources}, policy


def _sync_device_settings(conn, uid):
    """Persist an idempotent desired config revision for the next S3 report."""
    resolved, _cadence = _device_setting_targets(conn, uid)
    resolved = resolved["values"]
    desired = (
        round(float(resolved["temp_high"]) * 100),
        round(float(resolved["temp_low"]) * 100),
        round(float(resolved["rh_high"]) * 100),
        round(float(resolved["rh_low"]) * 100),
        int(resolved["normal_interval_sec"]),
        int(resolved["fast_interval_sec"]),
    )
    row = conn.execute(
        "SELECT revision,applied_revision,temp_high_centi,temp_low_centi,"
        "rh_high_centi,rh_low_centi,normal_interval_sec,fast_interval_sec "
        "FROM device_settings WHERE uid=?", (uid,)).fetchone()
    now = int(time.time())
    if row is None:
        conn.execute("""INSERT INTO device_settings(uid,revision,applied_revision,
            temp_high_centi,temp_low_centi,rh_high_centi,rh_low_centi,
            normal_interval_sec,fast_interval_sec,updated_ts,applied_ts)
            VALUES(?,1,0,?,?,?,?,?,?,?,0)""", (uid,) + desired + (now,))
    else:
        current = tuple(int(value) for value in row[2:])
        if current != desired:
            conn.execute("""UPDATE device_settings SET revision=revision+1,
                temp_high_centi=?,temp_low_centi=?,rh_high_centi=?,rh_low_centi=?,
                normal_interval_sec=?,fast_interval_sec=?,updated_ts=? WHERE uid=?""",
                         desired + (now, uid))


def _device_settings_state(conn, uid):
    _sync_device_settings(conn, uid)
    row = conn.execute(
        "SELECT revision,applied_revision,temp_high_centi,temp_low_centi,"
        "rh_high_centi,rh_low_centi,normal_interval_sec,fast_interval_sec,"
        "updated_ts,applied_ts FROM device_settings WHERE uid=?", (uid,)).fetchone()
    if not row:
        return None
    resolved, cadence = _device_setting_targets(
        conn, uid, effective_device_settings(conn, uid=uid))
    return {
        "revision": row[0], "applied_revision": row[1],
        "temp_high_centi": row[2], "temp_low_centi": row[3],
        "rh_high_centi": row[4], "rh_low_centi": row[5],
        "normal_interval_sec": row[6], "fast_interval_sec": row[7],
        "updated_ts": row[8], "applied_ts": row[9],
        "status": "applied" if row[0] == row[1] else "waiting_for_probe",
        "sources": resolved["sources"],
        "weather_cadence": cadence,
    }


def _remote_settings_payload(state):
    if state is None or state["revision"] == state["applied_revision"]:
        return None
    return {key: state[key] for key in (
        "revision", "temp_high_centi", "temp_low_centi", "rh_high_centi",
        "rh_low_centi", "normal_interval_sec", "fast_interval_sec")}


def effective_thresholds(conn, uid=None, pile_id=None, warehouse_id=None):
    """Resolve warehouse defaults, then pile overrides, then probe overrides."""
    if uid:
        warehouse_id = warehouse_for_uid(conn, uid)
    elif pile_id is not None and warehouse_id is None:
        row = conn.execute("SELECT warehouse_id FROM piles WHERE id=?",
                           (pile_id,)).fetchone()
        warehouse_id = row[0] if row else 1
    cfg = get_cfg(conn, warehouse_id or 1)
    values = {key: cfg[key] for key in THRESHOLD_KEYS}
    sources = {key: "warehouse" for key in THRESHOLD_KEYS}
    probe_overrides = None
    if uid:
        row = conn.execute(
            "SELECT pile_id," + ",".join(THRESHOLD_KEYS) +
            " FROM poles WHERE uid=? AND (deleted=0 OR deleted IS NULL)",
            (uid,)).fetchone()
        if row:
            if pile_id is None:
                pile_id = row[0]
            probe_overrides = dict(zip(THRESHOLD_KEYS, row[1:]))
    if pile_id is not None:
        row = conn.execute("SELECT " + ",".join(THRESHOLD_KEYS) +
                           " FROM piles WHERE id=?", (pile_id,)).fetchone()
        if row:
            for key, raw in zip(THRESHOLD_KEYS, row):
                if raw is not None:
                    values[key], sources[key] = float(raw), "pile"
    if probe_overrides:
        for key, raw in probe_overrides.items():
            if raw is not None:
                values[key], sources[key] = float(raw), "probe"
    return {"values": values, "sources": sources}


def _validate_all_threshold_scopes(conn, global_updates=None,
                                   pile_update=None, probe_update=None,
                                   warehouse_id=1):
    global_values = {key: float(value) for key, value in
                     get_cfg(conn, warehouse_id).items()
                     if key in THRESHOLD_KEYS}
    global_values.update(global_updates or {})
    _validate_thresholds(global_values)
    piles = {row[0]: dict(zip(THRESHOLD_KEYS, row[1:])) for row in conn.execute(
        "SELECT id," + ",".join(THRESHOLD_KEYS) + " FROM piles WHERE warehouse_id=?",
        (warehouse_id,))}
    poles = list(conn.execute("SELECT uid,pile_id," + ",".join(THRESHOLD_KEYS) +
                              " FROM poles WHERE warehouse_id=? AND "
                              "(deleted=0 OR deleted IS NULL)",
                              (warehouse_id,)))
    if pile_update:
        pile_id, updates = pile_update
        if pile_id not in piles:
            raise ValueError("粮堆不存在")
        piles[pile_id].update(updates)
    for pile_id, overrides in piles.items():
        pile_values = dict(global_values)
        pile_values.update({key: value for key, value in overrides.items()
                            if value is not None})
        _validate_thresholds(pile_values)
        for pole in poles:
            if pole[1] != pile_id:
                continue
            pole_values = dict(pile_values)
            pole_overrides = dict(zip(THRESHOLD_KEYS, pole[2:]))
            if probe_update and probe_update[0] == pole[0]:
                pole_overrides.update(probe_update[1])
            pole_values.update({key: value for key, value in pole_overrides.items()
                                if value is not None})
            _validate_thresholds(pole_values)
    for pole in poles:
        if pole[1] is not None:
            continue
        pole_values = dict(global_values)
        overrides = dict(zip(THRESHOLD_KEYS, pole[2:]))
        if probe_update and probe_update[0] == pole[0]:
            overrides.update(probe_update[1])
        pole_values.update({key: value for key, value in overrides.items()
                            if value is not None})
        _validate_thresholds(pole_values)
    if probe_update and not any(row[0] == probe_update[0] for row in poles):
        raise ValueError("探杆不存在")


def _validate_all_interval_scopes(conn, global_updates=None,
                                  pile_update=None, probe_update=None,
                                  warehouse_id=1):
    global_values = {key: int(value) for key, value in
                     get_cfg(conn, warehouse_id).items()
                     if key in INTERVAL_KEYS}
    global_values.update(global_updates or {})
    _validate_intervals(global_values)
    piles = {row[0]: dict(zip(INTERVAL_KEYS, row[1:])) for row in conn.execute(
        "SELECT id," + ",".join(INTERVAL_KEYS) +
        " FROM piles WHERE warehouse_id=?", (warehouse_id,))}
    poles = list(conn.execute("SELECT uid,pile_id," + ",".join(INTERVAL_KEYS) +
                              " FROM poles WHERE warehouse_id=? AND "
                              "(deleted=0 OR deleted IS NULL)",
                              (warehouse_id,)))
    if pile_update:
        pile_id, updates = pile_update
        if pile_id not in piles:
            raise ValueError("粮堆不存在")
        piles[pile_id].update(updates)
    for pile_id, overrides in piles.items():
        values = dict(global_values)
        values.update({key: int(value) for key, value in overrides.items()
                       if value is not None})
        _validate_intervals(values)
        for pole in poles:
            if pole[1] != pile_id:
                continue
            pole_values = dict(values)
            pole_overrides = dict(zip(INTERVAL_KEYS, pole[2:]))
            if probe_update and probe_update[0] == pole[0]:
                pole_overrides.update(probe_update[1])
            pole_values.update({key: int(value) for key, value in pole_overrides.items()
                                if value is not None})
            _validate_intervals(pole_values)
    for pole in poles:
        if pole[1] is not None:
            continue
        values = dict(global_values)
        overrides = dict(zip(INTERVAL_KEYS, pole[2:]))
        if probe_update and probe_update[0] == pole[0]:
            overrides.update(probe_update[1])
        values.update({key: int(value) for key, value in overrides.items()
                       if value is not None})
        _validate_intervals(values)
    if probe_update and not any(row[0] == probe_update[0] for row in poles):
        raise ValueError("探杆不存在")


def _validate_all_device_settings(conn, global_updates=None,
                                  pile_update=None, probe_update=None,
                                  warehouse_id=1):
    global_updates = global_updates or {}
    pile_update = pile_update or None
    probe_update = probe_update or None
    threshold_global = {key: value for key, value in global_updates.items()
                        if key in THRESHOLD_KEYS}
    interval_global = {key: value for key, value in global_updates.items()
                       if key in INTERVAL_KEYS}
    threshold_pile = ((pile_update[0], {key: value for key, value in
                      pile_update[1].items() if key in THRESHOLD_KEYS})
                      if pile_update else None)
    interval_pile = ((pile_update[0], {key: value for key, value in
                       pile_update[1].items() if key in INTERVAL_KEYS})
                     if pile_update else None)
    threshold_probe = ((probe_update[0], {key: value for key, value in
                       probe_update[1].items() if key in THRESHOLD_KEYS})
                       if probe_update else None)
    interval_probe = ((probe_update[0], {key: value for key, value in
                       probe_update[1].items() if key in INTERVAL_KEYS})
                      if probe_update else None)
    _validate_all_threshold_scopes(conn, threshold_global,
                                   threshold_pile, threshold_probe, warehouse_id)
    _validate_all_interval_scopes(conn, interval_global,
                                  interval_pile, interval_probe, warehouse_id)


def bat_pct(mv):
    """电量百分比：按 3.3V~4.2V 锂电池估算；无数据返回 None"""
    if not mv:
        return None
    p = (mv - 3300.0) / (4200.0 - 3300.0) * 100.0
    return max(0.0, min(100.0, p))


# ================= 告警状态机（内存） =================
# key: (uid, addr, ch) -> [over_streak, norm_streak, active, log_id]
_alarm_state = {}


def _ch_over(ch, cfg):
    """ch: (id, raw_value_float)。返回 (超限? , 告警文本)"""
    v = ch["value"]
    if ch["id"] == "temp":
        return (v > cfg["temp_high"] or v < cfg["temp_low"]), f"温度 {v:.2f}C"
    if ch["id"] == "rh":
        if cfg["rh_low"] > 0 and v < cfg["rh_low"]:
            return True, f"湿度 {v:.2f}%"
        return (v > cfg["rh_high"]), f"湿度 {v:.2f}%"
    return False, ""


def _update_alarm(uid, nodes, conn, cfg):
    """第二级告警：连续超限入 alert_log，恢复写 end_ts"""
    now = int(time.time())
    warehouse_id = warehouse_for_uid(conn, uid)
    for nd in nodes:
        addr = nd.get("addr")
        for key in ("temp", "rh"):
            v = nd.get(key)
            if v is None:
                continue
            ch = {"id": key, "value": v}
            over, txt = _ch_over(ch, cfg)
            st = _alarm_state.setdefault((uid, addr, key), [0, 0, 0, 0])
            if over:
                st[0] += 1
                st[1] = 0
                if st[2] == 0 and st[0] >= ALARM_TRIGGER_CNT:
                    if key == "temp":
                        th = cfg["temp_low"] if v < cfg["temp_low"] else cfg["temp_high"]
                    else:
                        th = cfg["rh_low"] if cfg["rh_low"] > 0 and v < cfg["rh_low"] else cfg["rh_high"]
                    cur = conn.execute(
                        "INSERT INTO alert_log(uid,node_addr,ch,value,th,begin_ts,active,"
                        "warehouse_id) VALUES(?,?,?,?,?,?,1,?)",
                        (uid, addr, key, v, th, now, warehouse_id))
                    conn.commit()
                    st[2] = 1
                    st[3] = cur.lastrowid
                    print(f"[ALERT] {uid} node{addr} {txt} -> 告警开始")
                elif st[2] == 1:
                    conn.execute("UPDATE alert_log SET value=? WHERE id=?",
                                 (v, st[3]))
                    conn.commit()
            else:
                st[1] += 1
                st[0] = 0
                if st[2] == 1 and st[1] >= ALARM_RECOVER_CNT:
                    conn.execute("UPDATE alert_log SET end_ts=?, active=0 WHERE id=?",
                                 (now, st[3]))
                    conn.commit()
                    st[2] = 0
                    print(f"[ALERT] {uid} node{addr} {key} 恢复正常")


def _probe_samples(conn, uid, addr, limit=2048):
    """Read measured temperature/RH history for exactly one physical node."""
    warehouse_id = warehouse_for_uid(conn, uid)
    rows = conn.execute(
        "SELECT ts,nodes_json FROM snapshots WHERE uid=? AND warehouse_id=? "
        "ORDER BY ts DESC,id DESC LIMIT ?", (uid, warehouse_id, limit)).fetchall()
    samples = []
    for ts, raw in rows:
        try:
            nodes = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for node in nodes:
            try:
                if int(node.get("addr", -1)) != int(addr):
                    continue
                samples.append({"ts": int(ts), "temp": node.get("temp"),
                                "rh": node.get("rh")})
                break
            except (TypeError, ValueError):
                continue
    return samples


def _probe_hourly_samples(conn, uid, addr, warehouse_id, limit=900):
    """Read one measured record nearest each UTC hour for weather calibration."""
    rows = conn.execute("""SELECT ts,nodes_json FROM (
        SELECT ts,nodes_json,
          ROW_NUMBER() OVER (
            PARTITION BY CAST(ts / 3600 AS INTEGER)
            ORDER BY ABS(ts - CAST(ts / 3600 AS INTEGER) * 3600),id DESC) AS rank
        FROM snapshots WHERE uid=? AND warehouse_id=?
          AND ts >= (SELECT COALESCE(MAX(ts),0) FROM snapshots
                     WHERE uid=? AND warehouse_id=?) - 45 * 86400
        ) WHERE rank=1 ORDER BY ts DESC LIMIT ?""",
        (uid, int(warehouse_id), uid, int(warehouse_id),
         max(1, min(int(limit), 900)))).fetchall()
    samples = []
    for ts, raw in rows:
        try:
            nodes = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        for node in nodes:
            try:
                if int(node.get("addr", -1)) != int(addr):
                    continue
                samples.append({"ts": int(ts), "temp": node.get("temp"),
                                "rh": node.get("rh")})
                break
            except (TypeError, ValueError):
                continue
    return samples


def _forecast_crossing(prediction, channel, cfg):
    if prediction.get("status") != "ready":
        return None
    for point in prediction.get("forecast", []):
        value = point.get("temperature_c") if channel == "temp" else point.get("rh_pct")
        if value is None:
            continue
        if channel == "temp":
            low, high = float(cfg["temp_low"]), float(cfg["temp_high"])
            over = value < low or value > high
            threshold = low if value < low else high
        else:
            low, high = float(cfg["rh_low"]), float(cfg["rh_high"])
            over = (low > 0.0 and value < low) or value > high
            threshold = low if low > 0.0 and value < low else high
        if over:
            return {"hour": int(point["hour"]), "value": float(value),
                    "threshold": float(threshold)}
    return None


def _update_forecast_alerts(conn, uid, nodes, cfg, now_ts):
    """Persist forecast-only alerts; unavailable history never implies recovery."""
    warehouse_id = warehouse_for_uid(conn, uid)
    for node in nodes:
        try:
            addr = int(node.get("addr"))
        except (TypeError, ValueError):
            continue
        prediction = predict_air_state(
            _probe_samples(conn, uid, addr), now_ts=now_ts)
        if prediction["status"] != "ready":
            continue
        for channel in ("temp", "rh"):
            crossing = _forecast_crossing(prediction, channel, cfg)
            active = conn.execute(
                "SELECT id FROM forecast_alert_log WHERE uid=? AND node_addr=? "
                "AND ch=? AND warehouse_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                (uid, addr, channel, warehouse_id)).fetchone()
            if crossing:
                if active:
                    conn.execute(
                        "UPDATE forecast_alert_log SET forecast_hour=?,value=?,th=?,last_ts=? "
                        "WHERE id=?", (crossing["hour"], crossing["value"],
                                        crossing["threshold"], now_ts, active[0]))
                else:
                    conn.execute(
                        "INSERT INTO forecast_alert_log(uid,node_addr,ch,forecast_hour,"
                        "value,th,begin_ts,last_ts,active,warehouse_id) "
                        "VALUES(?,?,?,?,?,?,?,?,1,?)",
                        (uid, addr, channel, crossing["hour"], crossing["value"],
                         crossing["threshold"], now_ts, now_ts, warehouse_id))
            elif active:
                conn.execute(
                    "UPDATE forecast_alert_log SET end_ts=?,active=0 WHERE id=?",
                    (now_ts, active[0]))


def _weather_forecast_is_complete(prediction):
    if not isinstance(prediction, dict) or prediction.get("status") != "ready":
        return False
    points = {int(point.get("hour", 0)): point
              for point in prediction.get("forecast", [])
              if isinstance(point, dict)}
    return all(hour in points and points[hour].get("temperature_c") is not None and
               points[hour].get("rh_pct") is not None for hour in (1, 3, 6))


def _sync_weather_forecast_alerts(conn, uid, warehouse_id, addresses,
                                  predictions, cfg, now_ts):
    """Persist only complete, validated weather-model alerts; unknown never clears."""
    for addr, prediction in zip(addresses, predictions):
        if not _weather_forecast_is_complete(prediction):
            continue
        for channel in ("temp", "rh"):
            db_channel = "weather_" + channel
            crossing = _forecast_crossing(prediction, channel, cfg)
            active = conn.execute(
                "SELECT id FROM forecast_alert_log WHERE uid=? AND node_addr=? "
                "AND ch=? AND warehouse_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                (uid, addr, db_channel, int(warehouse_id))).fetchone()
            if crossing:
                if active:
                    conn.execute(
                        "UPDATE forecast_alert_log SET forecast_hour=?,value=?,th=?,last_ts=? "
                        "WHERE id=?", (crossing["hour"], crossing["value"],
                                        crossing["threshold"], now_ts, active[0]))
                else:
                    conn.execute(
                        "INSERT INTO forecast_alert_log(uid,node_addr,ch,forecast_hour,value,th,"
                        "begin_ts,last_ts,active,warehouse_id) VALUES(?,?,?,?,?,?,?,?,1,?)",
                        (uid, addr, db_channel, crossing["hour"], crossing["value"],
                         crossing["threshold"], now_ts, now_ts, int(warehouse_id)))
            elif active:
                conn.execute(
                    "UPDATE forecast_alert_log SET end_ts=?,active=0 WHERE id=?",
                    (now_ts, active[0]))


# ================= 粮堆 + 三维热场插值（IDW）+ CFD 流线 =================
def _place_pile(cfg, piles, l, w):
    """粮堆自动布局：在仓库内扫描不与现有粮堆重叠的空位（含 GAP 间距）。
    多粮堆在算法上必须互不重叠——扫描全部候选取「离仓库中心最近」的
    （单堆自动居中），找不到空位返回 None，由调用方拒绝创建/回滚。"""
    wh_l, wh_w = cfg["wh_l"], cfg["wh_w"]
    GAP = 1.0                 # 粮堆之间最小间距（米）
    m = 0.5                   # 距仓壁最小边距（米）
    if l > wh_l - 2 * m or w > wh_w - 2 * m:
        l, w = min(l, wh_l - 2 * m), min(w, wh_w - 2 * m)
    step = 0.5

    def overlap_any(x, z):
        """是否与任一现有粮堆重叠（含 GAP 扩展）"""
        for p in piles:
            ox = max(0.0, min(x + l / 2 + GAP, p["x"] + p["l"] / 2 + GAP)
                     - max(x - l / 2 - GAP, p["x"] - p["l"] / 2 - GAP))
            if ox <= 1e-9:
                continue
            oz = max(0.0, min(z + w / 2 + GAP, p["z"] + p["w"] / 2 + GAP)
                     - max(z - w / 2 - GAP, p["z"] - p["w"] / 2 - GAP))
            if oz > 1e-9:
                return True
        return False

    best = None
    best_d = None
    x = m + l / 2
    while x + l / 2 <= wh_l - m + 1e-9:
        z = m + w / 2
        while z + w / 2 <= wh_w - m + 1e-9:
            if not overlap_any(x, z):
                d = abs(x - wh_l / 2) + abs(z - wh_w / 2)
                if best_d is None or d < best_d:
                    best_d, best = d, (x, z)
            z += step
        x += step
    if best:
        return (round(best[0], 2), round(best[1], 2))
    return None


def heap_geom(cfg, pile):
    """由粮堆重量/容重/底面积推算四棱锥台参数。
    粮面高度从底面 0 线性升到顶面（居中缩小 s 的矩形）h。
    体积 = A*h*(1+s²)/2  ->  h = 2V / (A(1+s²))，s 取 0.2（顶面 4% 面积）
    pile: {weight_kg, density, x, z, l, w}（x/z=堆中心绝对坐标，l/w=堆底长宽）
    """
    w = pile["weight_kg"]
    if w <= 0:
        return None
    l, ww = pile["l"], pile["w"]
    density = pile["density"] if pile["density"] > 0 else 750.0
    vol = w / density
    shape = pile.get("shape_type", "flat")
    repose = float(pile.get("repose_angle_deg", 30.0))
    max_height = cfg["wh_h"] * 0.85
    if shape == "silo":
        radius = float(pile.get("warehouse_diameter_m", min(l, ww))) / 2
        tangent = math.tan(math.radians(repose))
        natural_radius = (3.0 * vol / (math.pi * tangent)) ** (1.0 / 3.0)
        geometry_status = "estimated"
        if natural_radius <= radius:
            base_radius = natural_radius
            h = natural_radius * tangent
        else:
            base_radius = radius
            h = 3.0 * vol / (math.pi * radius * radius)
            geometry_status = "slope_exceeds_assumed_repose"
        if h > max_height:
            h = max_height
            geometry_status = "over_capacity"
        return {"x0": pile["x"] - l / 2, "z0": pile["z"] - ww / 2,
                "l": l, "w": ww, "h": h, "volume": vol, "s": 0.0,
                "shape_type": "silo", "base_radius_m": base_radius,
                "top_radius_m": 0.0, "repose_angle_deg": repose,
                "geometry_status": geometry_status}
    area = l * ww
    s = 0.2
    raw_height = 2.0 * vol / (area * (1.0 + s * s))
    h = min(raw_height, max_height)
    return {"x0": pile["x"] - l / 2, "z0": pile["z"] - ww / 2,
            "l": l, "w": ww, "h": h, "volume": vol, "s": s,
            "shape_type": "flat", "repose_angle_deg": repose,
            "geometry_status": "over_capacity" if raw_height > max_height else "estimated"}


def _heap_height(heap, x, z):
    """(x,z) 处粮面高度（米）。底面矩形 [x0,x0+l]x[z0,z0+w]，顶面居中缩小 s。"""
    l, w, h, s = heap["l"], heap["w"], heap["h"], heap["s"]
    if heap.get("shape_type") == "silo":
        cx, cz = heap["x0"] + l / 2, heap["z0"] + w / 2
        radius = heap["base_radius_m"]
        r = math.hypot(x - cx, z - cz)
        if r >= radius:
            return 0.0
        top = heap.get("top_radius_m", 0.0)
        if r <= top:
            return h
        return h * (radius - r) / max(radius - top, 1e-9)
    nx = abs(x - (heap["x0"] + l / 2)) / (l / 2)
    nz = abs(z - (heap["z0"] + w / 2)) / (w / 2)
    r = max(nx, nz)
    if r >= 1.0:
        return 0.0
    if r <= s:
        return h
    return h * (1.0 - r) / (1.0 - s)


def _idw_grid(cfg, pile_id=None, warehouse_id=1):
    """只用已校准的传感器物理 XYZ 坐标生成粮堆网格。"""
    conn = db()
    warehouse = conn.execute("SELECT shape_type,diameter_m,repose_angle_deg "
                             "FROM warehouses WHERE id=?",
                             (warehouse_id,)).fetchone()
    shape_type = warehouse[0] if warehouse else "flat"
    repose = warehouse[2] if warehouse else 30.0
    diameter = warehouse[1] if warehouse else min(cfg["wh_l"], cfg["wh_w"])
    if pile_id:
        r = conn.execute("SELECT id,name,weight_kg,density,x,z,l,w FROM piles "
                         "WHERE id=? AND warehouse_id=?",
                         (pile_id, warehouse_id)).fetchone()
        if not r:
            conn.close()
            return None, [], []
        pile = {"weight_kg": r[2], "density": r[3], "x": r[4], "z": r[5],
                "l": r[6], "w": r[7], "shape_type": shape_type,
                "warehouse_diameter_m": diameter, "repose_angle_deg": repose}
        if shape_type == "silo":
            pile.update({"x": diameter / 2, "z": diameter / 2,
                         "l": diameter, "w": diameter})
        rows = conn.execute("SELECT uid,name,pile_id FROM poles "
                            "WHERE pile_id=? AND warehouse_id=? "
                            "AND (deleted=0 OR deleted IS NULL)",
                            (pile_id, warehouse_id)).fetchall()
    else:
        pile = {"weight_kg": cfg["grain_weight"], "density": cfg["grain_density"],
                "x": cfg["wh_l"] / 2, "z": cfg["wh_w"] / 2,
                "l": cfg["wh_l"], "w": cfg["wh_w"],
                "shape_type": shape_type, "warehouse_diameter_m": diameter,
                "repose_angle_deg": repose}
        rows = conn.execute("SELECT uid,name,pile_id FROM poles "
                            "WHERE warehouse_id=? AND (deleted=0 OR deleted IS NULL)",
                            (warehouse_id,)).fetchall()
    heap = heap_geom(cfg, pile)
    samples = []
    now = int(time.time())
    for uid, name, row_pile_id in rows:
        positions = {row[0]: (row[1], row[2], row[3])
                     for row in conn.execute(
                         "SELECT addr,x,y,z FROM node_positions WHERE uid=?", (uid,))}
        r = conn.execute("SELECT ts,nodes_json FROM snapshots WHERE uid=? "
                         "AND warehouse_id=? ORDER BY id DESC LIMIT 1",
                         (uid, warehouse_id)).fetchone()
        if not r:
            continue
        for nd in json.loads(r[1]):
            if nd.get("temp") is None:
                continue
            addr = nd.get("addr", 0)
            pos = positions.get(addr)
            if not pos:
                continue
            x, y, z = pos
            if heap:
                surface = _heap_height(heap, x, z)
                if surface <= 0 or y < 0 or y > surface + 0.1:
                    continue
                depth = max(0.0, surface - y)
            else:
                depth = None
            samples.append({"x": float(x), "y": float(y), "z": float(z), "d": depth,
                            "t": nd["temp"], "h": nd.get("rh"),
                            "uid": uid, "name": name or uid, "addr": addr,
                            "pile_id": row_pile_id, "ts": int(r[0]),
                            "online": (now - int(r[0])) < OFFLINE_SEC,
                            "position_validated": True})
    conn.close()
    if heap is None or not samples:
        return heap, [], samples

    # Station 视觉层允许 12.5 cm 细分；默认配置仍为 0.5 m，
    # 只在显式请求更密网格时启用，不改变 AutoLink/传感器语义。
    step = max(0.125, min(cfg["heatmap_step"], 2.0))
    pts = []
    x = heap["x0"]
    while x <= heap["x0"] + heap["l"] + 1e-9:
        z = heap["z0"]
        while z <= heap["z0"] + heap["w"] + 1e-9:
            hh = _heap_height(heap, x, z)
            if hh > 0.01:
                d = 0.0
                while d <= hh + 1e-9:
                    pts.append((x, z, d))
                    d += step
            z += step
        x += step
    return heap, pts, samples


def _open_meteo(lat, lon, hours):
    """Read model-based outdoor weather context; never treat it as silo telemetry."""
    if lat is None or lon is None:
        return {"status": "not_configured", "source": None,
                "current": None, "series": [], "daily_series": []}
    try:
        query = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,weather_code",
            "hourly": "temperature_2m,relative_humidity_2m",
            "daily": ("temperature_2m_min,temperature_2m_max,"
                      "precipitation_probability_max,precipitation_sum,weather_code"),
            "forecast_days": 7, "timezone": "auto",
        })
        req = urllib.request.Request(
            "https://api.open-meteo.com/v1/forecast?" + query,
            headers={"User-Agent": "GrainSilo-Station/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        def to_timestamp(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    # Open-Meteo returns local wall time when timezone=auto.
                    # Convert it back to UTC so browser formatting is stable.
                    parsed = parsed.replace(tzinfo=timezone.utc)
                    return int(parsed.timestamp()) - int(
                        body.get("utc_offset_seconds") or 0)
                return int(parsed.timestamp())
            except ValueError:
                return None

        current_raw = body.get("current", {})
        current_temp = current_raw.get("temperature_2m")
        current_rh = current_raw.get("relative_humidity_2m")
        current = None
        if current_temp is not None or current_rh is not None:
            current = {"ts": to_timestamp(current_raw.get("time")),
                       "temperature_c": current_temp, "rh_pct": current_rh,
                       "weather_code": current_raw.get("weather_code")}
        hourly = body.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        humidities = hourly.get("relative_humidity_2m", [])
        series = []
        limit = max(1, min(int(hours), 48))
        for text, temp, humidity in zip(times, temps, humidities):
            if temp is None and humidity is None:
                continue
            ts = to_timestamp(text)
            if ts is None:
                continue
            series.append({"ts": ts, "temperature_c": temp, "rh_pct": humidity})
            if len(series) >= limit:
                break
        daily = body.get("daily", {})
        daily_series = []
        daily_times = daily.get("time", [])
        for index, day in enumerate(daily_times[:7]):
            daily_series.append({
                "date": str(day),
                "temperature_min_c": (daily.get("temperature_2m_min") or [])[index]
                    if index < len(daily.get("temperature_2m_min") or []) else None,
                "temperature_max_c": (daily.get("temperature_2m_max") or [])[index]
                    if index < len(daily.get("temperature_2m_max") or []) else None,
                "precipitation_probability_max_pct": (
                    (daily.get("precipitation_probability_max") or [])[index]
                    if index < len(daily.get("precipitation_probability_max") or [])
                    else None),
                "precipitation_sum_mm": (daily.get("precipitation_sum") or [])[index]
                    if index < len(daily.get("precipitation_sum") or []) else None,
                "weather_code": (daily.get("weather_code") or [])[index]
                    if index < len(daily.get("weather_code") or []) else None,
            })
        return {"status": "ready", "source": "Open-Meteo",
                "latitude": lat, "longitude": lon,
                "timezone": body.get("timezone"),
                "current": current, "series": series,
                "daily_series": daily_series}
    except Exception as exc:
        return {"status": "unavailable", "source": "Open-Meteo",
                "error": str(exc), "latitude": lat, "longitude": lon,
                "current": None, "series": [], "daily_series": []}


def _geocode_locations(name):
    """Look up candidate places through a fixed provider; never save coordinates here."""
    name = str(name or "").strip()
    if len(name) < 2 or len(name) > 120:
        raise ValueError("请输入 2–120 个字符的城市或地区名称")
    query = urllib.parse.urlencode({
        "name": name, "count": 8, "language": "zh", "format": "json",
    })
    req = urllib.request.Request(
        "https://geocoding-api.open-meteo.com/v1/search?" + query,
        headers={"User-Agent": "GrainSilo-Station/1.0"})
    with urllib.request.urlopen(req, timeout=4) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    results = []
    for item in body.get("results", [])[:8]:
        try:
            lat = float(item["latitude"])
            lon = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(lat) or not math.isfinite(lon) or not (
                -90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        results.append({
            "name": str(item.get("name") or "")[:100],
            "admin1": str(item.get("admin1") or "")[:100],
            "country": str(item.get("country") or "")[:100],
            "latitude": lat, "longitude": lon,
            "timezone": str(item.get("timezone") or "")[:80],
        })
    return results


def fetch_historical_weather(lat, lon, start_date, end_date):
    """Fetch hourly gridded model history; this is not a local weather station."""
    lat, lon = float(lat), float(lon)
    if (not math.isfinite(lat) or not math.isfinite(lon) or
            not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0):
        raise ValueError("纬度/经度无效")
    try:
        start = datetime.strptime(str(start_date), "%Y-%m-%d").date()
        end = datetime.strptime(str(end_date), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("日期必须使用 YYYY-MM-DD")
    if start > end or (end - start).days > 365:
        raise ValueError("历史天气范围最多 366 天，且开始日期不能晚于结束日期")
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "timezone": "UTC",
    })
    request = urllib.request.Request(
        "https://historical-forecast-api.open-meteo.com/v1/forecast?" + query,
        headers={"User-Agent": "GrainSilo-Station/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("历史天气响应超过大小限制")
    body = json.loads(raw.decode("utf-8"))
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    temperatures = hourly.get("temperature_2m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    dew_points = hourly.get("dew_point_2m") or []
    first_ts = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())
    after_last_ts = int(datetime.combine(end, datetime.min.time(), timezone.utc).timestamp()) + 86400

    def finite_or_none(value):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    records = []
    for index, text in enumerate(times):
        try:
            parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            ts = int(parsed.astimezone(timezone.utc).timestamp())
        except (TypeError, ValueError, OverflowError):
            continue
        if not first_ts <= ts < after_last_ts:
            continue
        temperature = finite_or_none(temperatures[index] if index < len(temperatures) else None)
        humidity = finite_or_none(humidities[index] if index < len(humidities) else None)
        dew_point = finite_or_none(dew_points[index] if index < len(dew_points) else None)
        if temperature is None and humidity is None and dew_point is None:
            continue
        if humidity is not None and not 0.0 <= humidity <= 100.0:
            humidity = None
        records.append({"ts": ts, "temperature_c": temperature,
                        "rh_pct": humidity, "dew_point_c": dew_point,
                        "source": "Open-Meteo Historical Forecast"})
    return records


def _weather_current_bundle(raw):
    """Decode current and daily fields, accepting pre-upgrade flat JSON rows."""
    value = json.loads(raw or "{}")
    if isinstance(value, dict) and ("daily_series" in value or "timezone" in value):
        return (value.get("current"), value.get("daily_series") or [],
                value.get("timezone"))
    return value, [], None


def _store_weather_run(conn, lat, lon, weather, fetched_ts=None, warehouse_id=1):
    if not weather or weather.get("status") != "ready":
        return
    fetched_ts = int(fetched_ts or time.time())
    conn.execute("""INSERT INTO warehouse_weather_runs(warehouse_id,lat_key,lon_key,
        run_slot,fetched_ts,current_json,series_json,source) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(warehouse_id,lat_key,lon_key,run_slot) DO UPDATE SET
        fetched_ts=excluded.fetched_ts,current_json=excluded.current_json,
        series_json=excluded.series_json,source=excluded.source""",
        (warehouse_id, f"{float(lat):.6f}", f"{float(lon):.6f}", fetched_ts // 3600,
         fetched_ts, json.dumps({"current": weather.get("current"),
                                 "daily_series": weather.get("daily_series", []),
                                 "timezone": weather.get("timezone")},
                                ensure_ascii=False),
         json.dumps(weather.get("series", []), ensure_ascii=False),
         str(weather.get("source") or "Open-Meteo")))


def _cached_weather_run(conn, lat, lon, now_ts=None, max_age_sec=900,
                        warehouse_id=1):
    """Reuse a recent location/model response so page polling cannot hammer the remote API."""
    if lat is None or lon is None:
        return None
    now_ts = int(now_ts or time.time())
    row = conn.execute("""SELECT fetched_ts,current_json,series_json,source
        FROM warehouse_weather_runs WHERE warehouse_id=? AND lat_key=? AND lon_key=? AND fetched_ts>=?
        ORDER BY fetched_ts DESC LIMIT 1""",
                       (warehouse_id, f"{float(lat):.6f}", f"{float(lon):.6f}",
                        now_ts - max(1, int(max_age_sec)))).fetchone()
    if not row:
        return None
    current, daily_series, tz_name = _weather_current_bundle(row[1])
    return {"status": "ready", "source": row[3], "latitude": float(lat),
            "longitude": float(lon), "timezone": tz_name,
            "current": current, "daily_series": daily_series,
            "series": json.loads(row[2]), "cached": True,
            "fetched_ts": row[0]}


def _archive_weather_once(now_ts=None):
    """Persist one model snapshot per configured warehouse while Station runs."""
    now_ts = int(now_ts or time.time())
    conn = db()
    locations = conn.execute(
        "SELECT warehouse_id,lat,lon FROM warehouse_weather ORDER BY warehouse_id"
    ).fetchall()
    conn.close()
    saved = 0
    for warehouse_id, lat, lon in locations:
        weather = _open_meteo(float(lat), float(lon), 24)
        if not weather or weather.get("status") != "ready":
            continue
        conn = db()
        _store_weather_run(conn, lat, lon, weather, now_ts,
                           warehouse_id=int(warehouse_id))
        conn.commit()
        conn.close()
        saved += 1
    return saved


def _weather_archive_worker():
    """Capture immediately, then once per UTC hour; failures never stop Station."""
    while True:
        try:
            count = _archive_weather_once()
            if count:
                print("[WEATHER] 已保存 %d 个仓库的天气模型快照" % count)
        except Exception as exc:
            print("[WEATHER] 定时留档失败：%s" % exc)
        next_hour = (int(time.time()) // 3600 + 1) * 3600
        time.sleep(max(1, next_hour - time.time()))


def _streamlines(heap, samples):
    """CFD 风格流线（pathlines）：从每个节点样本发射 8 条，
    径向扩散 + 螺旋扰动 + 缓慢上浮，逐步 IDW 插值温度。
    返回 [[[x,z,d,t],...], ...]（每条流线至少 4 个点）。"""
    lines = []
    for s in samples:
        base_ang = (s["x"] * 0.17 + s["z"] * 0.11) % (math.pi * 2)
        for k in range(8):
            ang = base_ang + math.pi / 4 * k
            px, pz, pd = s["x"], s["z"], s["d"]
            pts = []
            for i in range(18):
                t = i * 0.3
                nx = px + math.cos(ang) * t + math.sin(t * 2.1 + k * 1.7) * 0.34
                nz = pz + math.sin(ang) * t + math.cos(t * 1.7 + k * 2.3) * 0.34
                nd = max(0.0, pd - t * 0.16 + math.sin(t * 1.2 + k) * 0.1)
                hh = _heap_height(heap, nx, nz)
                if hh < 0.06 or nd > hh:
                    break
                v = _idw((nx, nz, nd), samples)
                if not v or v[0] is None:
                    break
                pts.append([round(nx, 2), round(nz, 2), round(nd, 2),
                            round(v[0], 2)])
                px, pz, pd = nx, nz, nd
            if len(pts) >= 4:
                lines.append(pts)
    return lines


def _idw(point, samples, k=8, p=2.0):
    """在物理 XYZ 坐标中做反距离加权；样本必须经过节点位置校准。"""
    near = sorted(samples,
                  key=lambda s: (s["x"] - point[0]) ** 2 + (s["y"] - point[1]) ** 2
                  + (s["z"] - point[2]) ** 2)[:k]
    wsum, wt, wh = 0.0, 0.0, 0.0
    for s in near:
        dd = math.sqrt((s["x"] - point[0]) ** 2 + (s["y"] - point[1]) ** 2
                       + (s["z"] - point[2]) ** 2)
        w = 1.0 / (dd ** p + 1e-6)
        wsum += w
        wt += w * s["t"]
        if s["h"] is not None:
            wh += w * s["h"]
    if wsum <= 0:
        return None
    return (wt / wsum, wh / wsum if wh > 0 else None)


# ================= HTTP 处理 =================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _select_warehouse_context(self):
        raw_id = self.headers.get("X-Warehouse-ID", "1")
        try:
            warehouse_id = int(raw_id)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "err": "invalid warehouse context"})
            return False
        conn = db()
        exists = conn.execute("SELECT 1 FROM warehouses WHERE id=? AND active=1",
                              (warehouse_id,)).fetchone()
        conn.close()
        if not exists:
            self._json(400, {"ok": False, "err": "warehouse not found or inactive"})
            return False
        self.warehouse_id = warehouse_id
        return True

    # ---------- POST ----------
    def do_POST(self):
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isascii() or not raw_length.isdecimal():
            self.close_connection = True
            self._json(400, {"ok": False, "err": "invalid content length"})
            return
        try:
            content_length = int(raw_length)
        except ValueError:
            self.close_connection = True
            self._json(400, {"ok": False, "err": "invalid content length"})
            return
        if content_length > MAX_REQUEST_BODY_BYTES:
            # Do not leave an unread request body on a reusable connection.
            self.close_connection = True
            self.send_response(413)
            self.send_header("Connection", "close")
            body = json.dumps({"ok": False, "err": "request body too large"},
                              ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._select_warehouse_context():
            return
        p = urlparse(self.path).path
        if p == "/api/v1/warehouses":
            self._warehouse_create()
        elif p.startswith("/api/v1/warehouses/"):
            self._warehouse_update(p)
        elif p == "/api/v1/crop-emc":
            self._crop_emc_post()
        elif p == "/api/v1/crop-profile":
            self._crop_profile_post()
        elif p == "/api/v1/snapshot":
            self._snapshot()
        elif p == "/api/v1/env":
            self._env_post()
        elif p == "/api/v1/weather/location":
            self._weather_location_post()
        elif p == "/api/v1/weather/backfill":
            self._weather_backfill_post()
        elif p == "/api/v1/config":
            self._config_post()
        elif p.startswith("/api/v1/poles/") and p.endswith("/cmd"):
            self._set_cmd(p)
        elif p.startswith("/api/v1/poles/") and p.endswith("/config"):
            self._set_config(p)
        elif p.startswith("/api/v1/poles/") and "/nodes/" in p and p.endswith("/position"):
            self._node_position(p)
        elif p == "/api/v1/piles":
            self._pile_create()
        elif p == "/api/v1/piles/relayout":
            self._pile_relayout()
        elif p == "/api/v1/poles":
            self._pole_create()
        elif p.startswith("/api/v1/piles/"):
            self._pile_update(p)
        elif p.startswith("/api/v1/trash/") and p.endswith("/restore"):
            self._trash_restore(p)
        elif p == "/api/v1/db/cleanup":
            self._db_cleanup()
        else:
            self._json(404, {"ok": False, "err": "not found"})

    def do_DELETE(self):
        if not self._select_warehouse_context():
            return
        p = urlparse(self.path).path
        if p.startswith("/api/v1/piles/"):
            pid = p.split("/")[4]
            conn = db()
            if not conn.execute("SELECT 1 FROM piles WHERE id=? AND warehouse_id=?",
                                (pid, self.warehouse_id)).fetchone():
                conn.close()
                self._json(404, {"ok": False, "err": "pile not found in current warehouse"})
                return
            affected = [row[0] for row in conn.execute(
                "SELECT uid FROM poles WHERE pile_id=? AND warehouse_id=?",
                (pid, self.warehouse_id)).fetchall()]
            conn.execute("UPDATE poles SET pile_id=NULL WHERE pile_id=? AND warehouse_id=?",
                         (pid, self.warehouse_id))
            conn.execute("DELETE FROM crop_profiles WHERE pile_id IN "
                         "(SELECT id FROM piles WHERE id=? AND warehouse_id=?)",
                         (pid, self.warehouse_id))
            conn.execute("DELETE FROM piles WHERE id=? AND warehouse_id=?",
                         (pid, self.warehouse_id))
            for uid in affected:
                _sync_device_settings(conn, uid)
            conn.commit()
            conn.close()
            print(f"[PILES] 删除粮堆 id={pid}（探杆已解绑）")
            self._json(200, {"ok": True})
        elif p.startswith("/api/v1/poles/") and "/nodes/" in p and p.endswith("/position"):
            self._node_position_delete(p)
        elif p.startswith("/api/v1/poles/"):
            self._pole_trash(p)
        elif p.startswith("/api/v1/trash/"):
            self._trash_purge(p)
        elif p == "/api/v1/trash":
            self._trash_clear()
        else:
            self._json(404, {"ok": False, "err": "not found"})

    # ---------- 探杆档案（手动添加） ----------
    def _pole_create(self):
        """手动添加探杆档案（3D 上直接显示；uid 在回收箱内则复活）
        body: {uid, name, x, y, z, spacing_m, pile_id}"""
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        uid = str(body.get("uid", "")).strip()
        if not uid:
            self._json(400, {"ok": False, "err": "uid 必填"})
            return
        conn = db()
        r = conn.execute("SELECT deleted,warehouse_id FROM poles WHERE uid=?",
                         (uid,)).fetchone()
        if r and not (r[0] or 0):
            conn.close()
            self._json(409, {"ok": False, "err": "探杆 %s 已存在" % uid})
            return
        if r and int(r[1] or 1) != self.warehouse_id:
            conn.close()
            self._json(409, {"ok": False,
                             "err": "探杆已归属其他仓库，请先在原仓库移入回收箱"})
            return
        name = str(body.get("name", "探杆 " + uid))[:40]
        try:
            x = float(body.get("x", 0) or 0)
            y = float(body.get("y", 0) or 0)
            z = float(body.get("z", 0) or 0)
            sp = float(body.get("spacing_m", 0.5) or 0.5)
        except (TypeError, ValueError):
            conn.close()
            self._json(400, {"ok": False, "err": "坐标/间距必须为数字"})
            return
        pile_id = body.get("pile_id")
        if pile_id not in (None, ""):
            try:
                pile_id = int(pile_id)
            except (TypeError, ValueError):
                conn.close()
                self._json(400, {"ok": False, "err": "pile_id must be an integer"})
                return
            if not conn.execute("SELECT 1 FROM piles WHERE id=? AND warehouse_id=?",
                                (pile_id, self.warehouse_id)).fetchone():
                conn.close()
                self._json(400, {"ok": False, "err": "粮堆不属于当前仓库"})
                return
        else:
            pile_id = None
        if r:  # 回收箱内复活
            conn.execute("UPDATE poles SET deleted=0, name=?, x=?, y=?, z=?, "
                         "spacing_m=?, pile_id=?, last_ts=0, status=1 WHERE uid=?",
                         (name, x, y, z, sp, pile_id, uid))
            conn.execute("DELETE FROM trash WHERE uid=? AND warehouse_id=?",
                         (uid, self.warehouse_id))
            msg = "recovered"
        else:
            conn.execute("INSERT INTO poles(uid,name,x,y,z,spacing_m,pile_id,warehouse_id) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (uid, name, x, y, z, sp, pile_id, self.warehouse_id))
            msg = "created"
        _sync_device_settings(conn, uid)
        conn.commit()
        conn.close()
        print(f"[POLES] 手动添加探杆 {uid} @({x},{y},{z}) {msg}")
        self._json(200, {"ok": True, "uid": uid, "msg": msg})

    def _node_position(self, path):
        """Save a manually surveyed physical coordinate for one reported node."""
        parts = path.split("/")
        try:
            uid, addr = unquote(parts[4]), int(parts[6])
        except (IndexError, ValueError):
            self._json(400, {"ok": False, "err": "invalid probe UID or node address"})
            return
        if not uid or len(uid) > 32 or not 1 <= addr <= 247:
            self._json(400, {"ok": False, "err": "probe UID or node address out of range"})
            return
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "err": "expect object"})
            return
        if body.get("calibrated") is False:
            self._node_position_delete(path)
            return
        try:
            x, y, z = (float(body[key]) for key in ("x", "y", "z"))
        except (KeyError, TypeError, ValueError):
            self._json(400, {"ok": False, "err": "x, y, z must be numeric"})
            return
        if not all(math.isfinite(value) for value in (x, y, z)):
            self._json(400, {"ok": False, "err": "coordinates must be finite"})
            return

        conn = db()
        pole = conn.execute("SELECT pile_id FROM poles WHERE uid=? AND warehouse_id=? "
                            "AND (deleted=0 OR deleted IS NULL)",
                            (uid, self.warehouse_id)).fetchone()
        if not pole:
            conn.close()
            self._json(404, {"ok": False, "err": "probe not found"})
            return
        latest = conn.execute("SELECT nodes_json FROM snapshots WHERE uid=? "
                              "AND warehouse_id=? ORDER BY id DESC LIMIT 1",
                              (uid, self.warehouse_id)).fetchone()
        reported = {node.get("addr") for node in json.loads(latest[0])} if latest else set()
        if addr not in reported:
            conn.close()
            self._json(404, {"ok": False, "err": "node address is not in the latest device report"})
            return
        cfg = get_cfg(conn, self.warehouse_id)
        warehouse = conn.execute("SELECT shape_type,diameter_m,repose_angle_deg "
                                 "FROM warehouses WHERE id=?",
                                 (self.warehouse_id,)).fetchone()
        inside_warehouse = (math.hypot(x - cfg["wh_l"] / 2,
                                       z - cfg["wh_w"] / 2) <= warehouse[1] / 2
                            if warehouse and warehouse[0] == "silo" else
                            0 <= x <= cfg["wh_l"] and 0 <= z <= cfg["wh_w"])
        if not (inside_warehouse and 0 <= y <= cfg["wh_h"]):
            conn.close()
            self._json(400, {"ok": False, "err": "node position must be inside warehouse dimensions"})
            return
        pile_id = pole[0]
        if pile_id is not None:
            r = conn.execute("SELECT weight_kg,density,x,z,l,w FROM piles "
                             "WHERE id=? AND warehouse_id=?",
                             (pile_id, self.warehouse_id)).fetchone()
            if r:
                pile = {"weight_kg": r[0], "density": r[1], "x": r[2], "z": r[3],
                        "l": r[4], "w": r[5],
                        "shape_type": warehouse[0] if warehouse else "flat",
                        "repose_angle_deg": warehouse[2] if warehouse else 30.0,
                        "warehouse_diameter_m": warehouse[1] if warehouse else 0}
                if pile["shape_type"] == "silo":
                    pile["x"] = pile["z"] = warehouse[1] / 2
                    pile["l"] = pile["w"] = warehouse[1]
                    inside_pile = math.hypot(x - pile["x"], z - pile["z"]) <= warehouse[1] / 2
                else:
                    inside_pile = (pile["x"] - pile["l"] / 2 <= x <= pile["x"] + pile["l"] / 2
                                   and pile["z"] - pile["w"] / 2 <= z <= pile["z"] + pile["w"] / 2)
                if not inside_pile:
                    conn.close()
                    self._json(400, {"ok": False, "err": "node position must be inside its assigned pile footprint"})
                    return
                heap = heap_geom(cfg, pile)
                if heap and y > _heap_height(heap, x, z) + 0.1:
                    conn.close()
                    self._json(400, {"ok": False, "err": "node position is above the configured grain surface"})
                    return
        now = int(time.time())
        conn.execute("INSERT OR REPLACE INTO node_positions(uid,addr,x,y,z,calibrated_ts) "
                     "VALUES(?,?,?,?,?,?)", (uid, addr, x, y, z, now))
        write_audit(conn, "node.position.calibrate", "node", f"{uid}:{addr}",
                    {"x": x, "y": y, "z": z}, warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        self._json(200, {"ok": True, "uid": uid, "addr": addr,
                         "position": {"x": x, "y": y, "z": z,
                                      "calibrated": True, "calibrated_ts": now}})

    def _node_position_delete(self, path):
        """Clear node coordinate calibration without deleting telemetry history."""
        parts = path.split("/")
        try:
            uid, addr = unquote(parts[4]), int(parts[6])
        except (IndexError, ValueError):
            self._json(400, {"ok": False, "err": "invalid probe UID or node address"})
            return
        if not uid or not 1 <= addr <= 247:
            self._json(400, {"ok": False, "err": "probe UID or node address out of range"})
            return
        conn = db()
        owner = conn.execute("SELECT warehouse_id FROM poles WHERE uid=?", (uid,)).fetchone()
        if not owner or int(owner[0] or 1) != self.warehouse_id:
            conn.close()
            self._json(404, {"ok": False, "err": "probe not found in current warehouse"})
            return
        conn.execute("DELETE FROM node_positions WHERE uid=? AND addr=?", (uid, addr))
        write_audit(conn, "node.position.clear", "node", f"{uid}:{addr}", {},
                    warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        self._json(200, {"ok": True, "uid": uid, "addr": addr, "calibrated": False})

    # ---------- 回收箱 ----------
    def _pole_trash(self, p):
        """软删除探杆：档案移入回收箱（数据保留），poles.deleted=1 不再显示"""
        uid = p.split("/")[4]
        conn = db()
        r = conn.execute("SELECT name,pile_id,bat_mv FROM poles WHERE uid=? "
                         "AND warehouse_id=?", (uid, self.warehouse_id)).fetchone()
        if not r:
            conn.close()
            self._json(404, {"ok": False, "err": "pole not found"})
            return
        pile_name = None
        if r[1]:
            pile_name = conn.execute("SELECT name FROM piles WHERE id=? AND warehouse_id=?",
                                     (r[1], self.warehouse_id)).fetchone()
            pile_name = pile_name[0] if pile_name else None
        cnt = conn.execute("SELECT COUNT(*) FROM snapshots WHERE uid=? AND warehouse_id=?",
                           (uid, self.warehouse_id)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO trash(uid,name,pile_id,pile_name,"
                     "snap_count,bat_mv,deleted_ts,warehouse_id) VALUES(?,?,?,?,?,?,?,?)",
                     (uid, r[0], r[1], pile_name, cnt, r[2] or 0,
                      int(time.time()), self.warehouse_id))
        conn.execute("UPDATE poles SET deleted=1, pile_id=NULL WHERE uid=? AND warehouse_id=?",
                     (uid, self.warehouse_id))
        conn.commit()
        conn.close()
        print(f"[TRASH] 探杆 {uid} 已移入回收箱（快照 {cnt} 条保留，可恢复）")
        self._json(200, {"ok": True, "snap_count": cnt})

    def _trash_restore(self, p):
        """回收箱恢复：档案回 poles，deleted=0 重新显示"""
        uid = p.split("/")[4]
        conn = db()
        r = conn.execute("SELECT name,pile_id FROM trash WHERE uid=? AND warehouse_id=?",
                          (uid, self.warehouse_id)).fetchone()
        if not r:
            conn.close()
            self._json(404, {"ok": False, "err": "not in trash"})
            return
        conn.execute("UPDATE poles SET deleted=0, pile_id=?, status=1, last_ts=? "
                     "WHERE uid=? AND warehouse_id=?",
                     (r[1], int(time.time()), uid, self.warehouse_id))
        conn.execute("DELETE FROM trash WHERE uid=? AND warehouse_id=?",
                     (uid, self.warehouse_id))
        conn.commit()
        conn.close()
        print(f"[TRASH] 探杆 {uid} 已恢复（回绑粮堆 id={r[1]}）")
        self._json(200, {"ok": True})

    def _trash_purge(self, p):
        """彻底删除：删档案 + 全部快照 + 节点记忆 + 告警 + 回收箱记录"""
        uid = p.split("/")[4]
        conn = db()
        r = conn.execute("SELECT name FROM trash WHERE uid=? AND warehouse_id=?",
                         (uid, self.warehouse_id)).fetchone()
        if not r:
            conn.close()
            self._json(404, {"ok": False, "err": "not in trash"})
            return
        snap_cnt = conn.execute("DELETE FROM snapshots WHERE uid=? AND warehouse_id=?",
                                (uid, self.warehouse_id)).rowcount
        conn.execute("DELETE FROM pole_nodes WHERE uid=?", (uid,))
        conn.execute("DELETE FROM alert_log WHERE uid=? AND warehouse_id=?",
                     (uid, self.warehouse_id))
        conn.execute("DELETE FROM poles WHERE uid=? AND warehouse_id=?",
                     (uid, self.warehouse_id))
        conn.execute("DELETE FROM trash WHERE uid=? AND warehouse_id=?",
                     (uid, self.warehouse_id))
        conn.commit()
        conn.close()
        _node_seen.pop(uid, None)
        for k in list(_node_last):
            if k[0] == uid:
                del _node_last[k]
        print(f"[TRASH] 探杆 {uid} 已彻底删除（快照 {snap_cnt} 条已清理）")
        self._json(200, {"ok": True, "snap_count": snap_cnt})

    def _trash_clear(self):
        """清空回收箱：全部彻底删除（防硬盘膨胀）"""
        conn = db()
        rows = conn.execute("SELECT uid FROM trash WHERE warehouse_id=?",
                             (self.warehouse_id,)).fetchall()
        total = 0
        for (uid,) in rows:
            total += conn.execute("DELETE FROM snapshots WHERE uid=? AND warehouse_id=?",
                                  (uid, self.warehouse_id)).rowcount
            conn.execute("DELETE FROM pole_nodes WHERE uid=?", (uid,))
            conn.execute("DELETE FROM alert_log WHERE uid=? AND warehouse_id=?",
                         (uid, self.warehouse_id))
            conn.execute("DELETE FROM poles WHERE uid=? AND warehouse_id=?",
                         (uid, self.warehouse_id))
            conn.execute("DELETE FROM trash WHERE uid=? AND warehouse_id=?",
                         (uid, self.warehouse_id))
            _node_seen.pop(uid, None)
            for k in list(_node_last):
                if k[0] == uid:
                    del _node_last[k]
        conn.commit()
        conn.close()
        print(f"[TRASH] 回收箱已清空（{len(rows)} 杆，快照 {total} 条已清理）")
        self._json(200, {"ok": True, "poles": len(rows), "snap_count": total})

    def _trash_list(self):
        conn = db()
        rows = conn.execute("SELECT uid,name,pile_name,snap_count,bat_mv,deleted_ts "
                            "FROM trash WHERE warehouse_id=? ORDER BY deleted_ts DESC",
                            (self.warehouse_id,)).fetchall()
        conn.close()
        self._json(200, [{"uid": r[0], "name": r[1], "pile_name": r[2],
                          "snap_count": r[3], "bat_mv": r[4], "deleted_ts": r[5]}
                         for r in rows])

    def _db_cleanup(self):
        """清理历史快照（防硬盘膨胀）：按 body 条件删除早期数据。
        body: {uid?: 只清该杆, keep?: 保留最近条数, keep_hours?: 保留最近小时数,
               all?: 清空全部快照}"""
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        conn = db()
        if body.get("all"):
            cnt = conn.execute("DELETE FROM snapshots WHERE warehouse_id=?",
                               (self.warehouse_id,)).rowcount
        elif body.get("keep_hours"):
            since = int(time.time()) - int(body["keep_hours"]) * 3600
            if body.get("uid"):
                cnt = conn.execute("DELETE FROM snapshots WHERE uid=? AND ts<? "
                                   "AND warehouse_id=?",
                                   (body["uid"], since, self.warehouse_id)).rowcount
            else:
                cnt = conn.execute("DELETE FROM snapshots WHERE ts<? AND warehouse_id=?",
                                   (since, self.warehouse_id)).rowcount
        elif "keep" in body:
            keep = max(0, int(body["keep"]))
            uid = body.get("uid")
            if not uid:
                conn.close()
                self._json(400, {"ok": False, "err": "keep 需要指定 uid"})
                return
            if not conn.execute("SELECT 1 FROM poles WHERE uid=? AND warehouse_id=?",
                                (uid, self.warehouse_id)).fetchone():
                conn.close()
                self._json(404, {"ok": False, "err": "探杆不属于当前仓库"})
                return
            keep_ids = [r[0] for r in conn.execute(
                "SELECT id FROM snapshots WHERE uid=? AND warehouse_id=? "
                "ORDER BY id DESC LIMIT ?", (uid, self.warehouse_id, keep)).fetchall()]
            if keep_ids:
                ph = ",".join("?" * len(keep_ids))
                cnt = conn.execute(
                    f"DELETE FROM snapshots WHERE uid=? AND warehouse_id=? "
                    f"AND id NOT IN ({ph})", [uid, self.warehouse_id] + keep_ids).rowcount
            else:
                cnt = conn.execute("DELETE FROM snapshots WHERE uid=? AND warehouse_id=?",
                                   (uid, self.warehouse_id)).rowcount
        else:
            conn.close()
            self._json(400, {"ok": False, "err": "缺少清理条件"})
            return
        conn.commit()
        conn.close()
        print(f"[DB] 清理快照 {cnt} 条 {body}")
        self._json(200, {"ok": True, "deleted": cnt})

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8", "replace"))

    def _read_bounded_json(self, max_bytes=4096):
        n = int(self.headers.get("Content-Length", 0))
        if n < 1 or n > max_bytes:
            raise ValueError("invalid request size")
        return json.loads(self.rfile.read(n).decode("utf-8", "replace"))

    def _snapshot(self):
        try:
            snap = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        uid = str(snap.get("pole_uid", ""))
        if not uid:
            self._json(400, {"ok": False, "err": "no pole_uid"})
            return
        now = _snapshot_time()
        conn = db()
        row = conn.execute("SELECT pending_cmd,deleted,warehouse_id FROM poles WHERE uid=?",
                           (uid,)).fetchone()
        cmd = None
        if row is None:
            conn.execute("INSERT INTO poles(uid,name,last_ts,status,warehouse_id) "
                         "VALUES(?,?,?,1,?)",
                         (uid, "探杆 " + uid, now, self.warehouse_id))
            print(f"[NEW POLE] uid={uid} 首次识别 -> 建档")
        else:
            self.warehouse_id = int(row[2] or 1)
            conn.execute("UPDATE poles SET last_ts=?, status=1 WHERE uid=?", (now, uid))
            if row[1]:
                # 在回收箱里的杆重新上报 -> 自动复活（档案回滚，删除时间更新）
                conn.execute("UPDATE poles SET deleted=0, pile_id=NULL WHERE uid=?",
                             (uid,))
                conn.execute("DELETE FROM trash WHERE uid=?", (uid,))
                print(f"[TRASH] uid={uid} 重新上报 -> 自动恢复")
            if row[0]:
                cmd = row[0]
                conn.execute("UPDATE poles SET pending_cmd='' WHERE uid=?", (uid,))
        bat = snap.get("bat_mv")
        if bat:
            conn.execute("UPDATE poles SET bat_mv=? WHERE uid=?", (float(bat), uid))
        _store_minute_snapshot(conn, uid, snap, now)
        # 节点级在线跟踪（记忆：曾见过的节点地址永久保留，落库防重启丢失）
        remember_nodes(conn, uid, snap.get("nodes", []), now)
        policy_warehouse_id = int(self.warehouse_id or 1)
        _evaluate_weather_cadence(conn, uid, policy_warehouse_id, now)
        cfg = effective_thresholds(conn, uid=uid)["values"]
        _update_alarm(uid, snap.get("nodes", []), conn, cfg)
        _update_forecast_alerts(conn, uid, snap.get("nodes", []), cfg, now)
        _sync_device_settings(conn, uid)
        reported_revision = snap.get("remote_config_revision")
        if isinstance(reported_revision, int) and not isinstance(reported_revision, bool):
            desired_revision = conn.execute(
                "SELECT revision FROM device_settings WHERE uid=?", (uid,)
            ).fetchone()
            if desired_revision and reported_revision == desired_revision[0]:
                conn.execute("UPDATE device_settings SET applied_revision=?,applied_ts=? "
                             "WHERE uid=?", (reported_revision, now, uid))
        settings_state = _device_settings_state(conn, uid)
        settings_payload = _remote_settings_payload(settings_state)
        conn.commit()
        conn.close()
        mode = "DEBUG" if snap.get("mode") == 1 else "LOWPOWER"
        print(f"[SNAP] uid={uid} mode={mode} alarm={snap.get('alarm')} "
              f"nodes={len(snap.get('nodes', []))} bat={bat} cmd={cmd}")
        self._json(200, {"ok": True, "cmd": cmd, "settings": settings_payload})

    def _env_post(self):
        """环境监测预留口：任意 JSON 对象直接存库，前端原样显示"""
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        if "data" in body and isinstance(body["data"], dict):
            body = body["data"]
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "err": "expect object"})
            return
        conn = db()
        now = int(time.time())
        known = {r[0] for r in conn.execute(
            "SELECT key FROM environment_registry").fetchall()}
        for key in body:
            if key in ("ts", "source") or key in known:
                continue
            if isinstance(body[key], (int, float)):
                conn.execute("INSERT OR IGNORE INTO environment_registry"
                             "(key,label,unit,enabled,source,updated_ts) VALUES(?,?,?,?,?,?)",
                             (str(key), str(key), "", 1, "payload", now))
        conn.execute("INSERT INTO env(ts,data_json,warehouse_id) VALUES(?,?,?)",
                     (now, json.dumps(body, ensure_ascii=False), self.warehouse_id))
        write_audit(conn, "environment.ingest", "environment", "",
                    {"keys": sorted(str(k) for k in body)},
                    warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        print(f"[ENV] 环境上报 {body}")
        self._json(200, {"ok": True})

    def _config_post(self):
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "err": "expect object"})
            return
        if {"wh_l", "wh_w", "wh_h"}.intersection(body):
            self._json(400, {"ok": False, "err":
                "请在“仓库档案”按仓型编辑尺寸，避免模型与仓房档案不一致"})
            return
        conn = db()
        try:
            threshold_updates = _threshold_updates(body, allow_clear=False)
            interval_updates = _interval_updates(body, allow_clear=False)
            _validate_all_device_settings(
                conn, global_updates={**threshold_updates, **interval_updates},
                warehouse_id=self.warehouse_id)
        except ValueError as error:
            conn.close()
            self._json(400, {"ok": False, "err": str(error)})
            return
        n = 0
        changed = []
        for k, v in body.items():
            if k in CONFIG_KEYS:
                conn.execute("INSERT INTO warehouse_config(warehouse_id,key,value) "
                             "VALUES(?,?,?) ON CONFLICT(warehouse_id,key) DO UPDATE "
                             "SET value=excluded.value",
                             (self.warehouse_id, k, str(v)))
                if self.warehouse_id == 1:
                    conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
                                 (k, str(v)))
                n += 1
                changed.append(k)
        if changed:
            detail = {"keys": sorted(changed), "count": n}
            write_audit(conn, "config.update", "config", "", detail,
                        warehouse_id=self.warehouse_id)
            conn.execute("INSERT INTO config_revisions(ts,actor,detail_json,warehouse_id) "
                         "VALUES(?,?,?,?)",
                         (int(time.time()), "station-api",
                          json.dumps(detail, ensure_ascii=False, sort_keys=True),
                          self.warehouse_id))
            for (uid,) in conn.execute(
                    "SELECT uid FROM poles WHERE warehouse_id=? AND "
                    "(deleted=0 OR deleted IS NULL)", (self.warehouse_id,)):
                _sync_device_settings(conn, uid)
        conn.commit()
        conn.close()
        print(f"[CFG] 全局配置更新 {n} 项 {body}")
        self._json(200, {"ok": True, "updated": n})

    def _set_cmd(self, p):
        uid = p.split("/")[4]
        try:
            cmd = self._read_json().get("cmd")
        except Exception:
            self._json(400, {"ok": False})
            return
        conn = db()
        cur = conn.execute("UPDATE poles SET pending_cmd=? WHERE uid=? AND warehouse_id=?",
                            (cmd, uid, self.warehouse_id))
        conn.commit()
        conn.close()
        print(f"[CMD] 指令缓存 uid={uid} cmd={cmd}")
        self._json(200, {"ok": cur.rowcount > 0})

    def _set_config(self, p):
        uid = p.split("/")[4]
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False})
            return
        conn = db()
        cur = conn.execute("SELECT pile_id FROM poles WHERE uid=? AND warehouse_id=?",
                           (uid, self.warehouse_id)).fetchone()
        if cur is None:
            conn.close()
            self._json(404, {"ok": False, "err": "pole not found"})
            return
        try:
            threshold_updates = _threshold_updates(body)
            interval_updates = _interval_updates(body)
            if body.get("pile_id") not in (None, "", 0, "0") and not conn.execute(
                    "SELECT 1 FROM piles WHERE id=? AND warehouse_id=?",
                    (body.get("pile_id"), self.warehouse_id)).fetchone():
                raise ValueError("粮堆不属于当前仓库")
            if threshold_updates or interval_updates:
                _validate_all_device_settings(
                    conn, probe_update=(uid, {**threshold_updates,
                                              **interval_updates}),
                    warehouse_id=self.warehouse_id)
        except ValueError as error:
            conn.close()
            self._json(400, {"ok": False, "err": str(error)})
            return
        fields = ["name", "x", "y", "z", "spacing_m", "pile_id"]
        sets, vals = [], []
        for f in fields:
            if f in body:
                if f == "pile_id" and not body[f]:
                    sets.append("pile_id=NULL")   # 0/null -> 解绑粮堆
                else:
                    sets.append(f"{f}=?")
                    vals.append(body[f])
        if sets:
            conn.execute(f"UPDATE poles SET {','.join(sets)} WHERE uid=? AND warehouse_id=?",
                         vals + [uid, self.warehouse_id])
        for key, value in threshold_updates.items():
            conn.execute(f"UPDATE poles SET {key}=? WHERE uid=? AND warehouse_id=?",
                         (value, uid, self.warehouse_id))
        for key, value in interval_updates.items():
            conn.execute(f"UPDATE poles SET {key}=? WHERE uid=? AND warehouse_id=?",
                         (value, uid, self.warehouse_id))
        if threshold_updates or interval_updates:
            _sync_device_settings(conn, uid)
        write_audit(conn, "pole.update", "pole", uid,
                    {"keys": sorted(k for k in body if k in fields or k in
                                     THRESHOLD_KEYS)}, warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        print(f"[CFG] uid={uid} 档案已更新 {body}")
        self._json(200, {"ok": True})

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, u.query
        # Keep warehouse discovery available if the browser cached a stale ID.
        if p == "/api/v1/warehouses":
            self.warehouse_id = 1
        elif not self._select_warehouse_context():
            return
        if p == "/api/ping":
            self._json(200, {"ok": True})
        elif p == "/api/v1/health":
            self._health()
        elif p == "/api/v1/warehouses":
            self._warehouses_list()
        elif p == "/api/v1/actuators":
            self._actuator_capabilities()
        elif p == "/api/v1/poles":
            self._poles()
        elif p == "/api/v1/config":
            self._config_get()
        elif p == "/api/v1/env/registry":
            self._env_registry()
        elif p == "/api/v1/env/latest":
            self._env_latest()
        elif p == "/api/v1/weather/location":
            self._weather_location_get()
        elif p == "/api/v1/weather/geocode":
            self._weather_geocode(q)
        elif p == "/api/v1/weather/history":
            self._weather_history(q)
        elif p == "/api/v1/env":
            self._env_history(q)
        elif p == "/api/v1/heatmap":
            self._heatmap(q)
        elif p == "/api/v1/forecast":
            self._forecast(q)
        elif p == "/api/v1/probe-forecast":
            self._probe_forecast(q)
        elif p.startswith("/api/v1/poles/") and p.endswith("/data"):
            self._pole_data(p, q)
        elif p.startswith("/api/v1/poles/"):
            self._pole(p)
        elif p == "/api/v1/alerts":
            self._alerts()
        elif p == "/api/v1/audit":
            self._audit(q)
        elif p == "/api/v1/piles":
            self._piles()
        elif p == "/api/v1/crop-profile":
            self._crop_profile_get(q)
        elif p == "/api/v1/db":
            self._db_view(q)
        elif p == "/api/v1/db/export.csv":
            self._db_export(q)
        elif p == "/api/v1/db/export.xlsx":
            self._db_export(q, xlsx=True)
        elif p == "/api/v1/trash":
            self._trash_list()
        elif p.startswith("/assets/") or p in (
                "/", "/index.html", "/pole.html", "/db.html", "/trash.html",
                "/crop-models.html", "/piles.html", "/probes.html",
                "/environment.html", "/alerts.html", "/warehouses.html"):
            self._static(p)
        else:
            self._json(404, {"ok": False, "err": "not found"})

    @staticmethod
    def _latest_snapshot(conn, uid):
        row = conn.execute("SELECT ts,mode,alarm,nodes_json,bat_mv,reporting_json FROM snapshots "
                           "WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not row:
            return None
        return {"ts": row[0], "mode": row[1], "alarm": row[2],
                "nodes": json.loads(row[3]), "bat_mv": row[4],
                "reporting": json.loads(row[5] or "{}")}

    @staticmethod
    def _node_positions(conn, uid):
        rows = conn.execute("SELECT addr,x,y,z,calibrated_ts FROM node_positions "
                            "WHERE uid=? ORDER BY addr", (uid,)).fetchall()
        return {str(row[0]): {"x": row[1], "y": row[2], "z": row[3],
                              "calibrated": True, "calibrated_ts": row[4]}
                for row in rows}

    def _node_states(self, uid, snap, node_offline):
        """节点级在线状态：记忆地址 + 最后上报时间 + 最新数值"""
        now = int(time.time())
        addr_now = {}
        if snap:
            for nd in snap["nodes"]:
                if nd.get("addr") is not None:
                    addr_now[nd["addr"]] = nd
        out = []
        for addr in sorted(_node_seen.get(uid, {})):
            last = _node_last.get((uid, addr), 0)
            nd = addr_now.get(addr, {})
            out.append({
                "addr": addr,
                "online": last and (now - last) < node_offline,
                "last_ts": last,
                "temp": nd.get("temp"),
                "rh": nd.get("rh"),
            })
        return out

    def _warehouses_list(self):
        conn = db()
        rows = conn.execute("SELECT id,name,shape_type,length_m,width_m,height_m,"
                            "diameter_m,repose_angle_deg,active,created_ts FROM warehouses "
                            "WHERE active=1 ORDER BY id").fetchall()
        conn.close()
        self._json(200, {"ok": True, "data": [{
            "id": row[0], "name": row[1], "shape_type": row[2],
            "length_m": row[3], "width_m": row[4], "height_m": row[5],
            "diameter_m": row[6], "repose_angle_deg": row[7],
            "active": bool(row[8]), "created_ts": row[9],
        } for row in rows]})

    @staticmethod
    def _valid_warehouse_dimensions(body, current=None):
        current = current or {}
        shape = body.get("shape_type", current.get("shape_type", "flat"))
        if shape not in ("flat", "silo"):
            raise ValueError("仓型必须为平房仓或圆筒仓")

        def dimension(key, fallback):
            raw = body.get(key, fallback)
            if isinstance(raw, bool):
                raise ValueError("仓房尺寸必须为数值")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ValueError("仓房尺寸必须为数值")
            if not math.isfinite(value) or not 0.5 <= value <= 500.0:
                raise ValueError("仓房尺寸须在 0.5 至 500 米之间")
            return value

        if shape == "silo":
            diameter = dimension("diameter_m", current.get("diameter_m", 12.0))
            height = dimension("height_m", current.get("height_m", 18.0))
            length, width = diameter, diameter
        else:
            length = dimension("length_m", current.get("length_m", 20.0))
            width = dimension("width_m", current.get("width_m", 10.0))
            height = dimension("height_m", current.get("height_m", 8.0))
            diameter = dimension("diameter_m", current.get("diameter_m", 12.0))
        raw_angle = body.get("repose_angle_deg", current.get("repose_angle_deg", 30.0))
        if isinstance(raw_angle, bool):
            raise ValueError("估算休止角必须为数值")
        try:
            repose = float(raw_angle)
        except (TypeError, ValueError):
            raise ValueError("估算休止角必须为数值")
        if not math.isfinite(repose) or not 10.0 <= repose <= 60.0:
            raise ValueError("估算休止角须在 10° 至 60° 之间")
        return shape, length, width, height, diameter, repose

    def _warehouse_create(self):
        try:
            body = self._read_bounded_json(2048)
            if not isinstance(body, dict):
                raise ValueError("expect object")
            name = str(body.get("name", "")).strip()
            if not name or len(name) > 64:
                raise ValueError("仓库名称不能为空且不能超过 64 个字符")
            shape, length, width, height, diameter, repose = self._valid_warehouse_dimensions(body)
        except (ValueError, TypeError) as error:
            self._json(400, {"ok": False, "err": str(error)})
            return
        conn = db()
        now = int(time.time())
        try:
            cur = conn.execute("INSERT INTO warehouses(name,shape_type,length_m,width_m,"
                               "height_m,diameter_m,repose_angle_deg,created_ts) "
                               "VALUES(?,?,?,?,?,?,?,?)",
                               (name, shape, length, width, height, diameter, repose, now))
            warehouse_id = int(cur.lastrowid)
            defaults = dict(DEFAULT_TH, **DEFAULT_GLOBAL)
            defaults.update({"wh_l": str(length), "wh_w": str(width),
                             "wh_h": str(height), "grain_weight": "0.0"})
            conn.executemany("INSERT INTO warehouse_config(warehouse_id,key,value) "
                             "VALUES(?,?,?)", [(warehouse_id, key, value)
                                                for key, value in defaults.items()])
            write_audit(conn, "warehouse.created", "warehouse", warehouse_id,
                        {"name": name, "shape_type": shape},
                        warehouse_id=self.warehouse_id)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            self._json(409, {"ok": False, "err": "仓库名称已存在"})
            return
        conn.close()
        self._json(201, {"ok": True, "id": warehouse_id, "name": name,
                         "shape_type": shape, "repose_angle_deg": repose})

    def _warehouse_update(self, path):
        try:
            warehouse_id = int(path.rsplit("/", 1)[1])
            body = self._read_bounded_json(2048)
            if not isinstance(body, dict):
                raise ValueError("expect object")
            if warehouse_id != self.warehouse_id:
                raise ValueError("只能编辑当前选中的仓库")
            conn = db()
            row = conn.execute("SELECT name,shape_type,length_m,width_m,height_m,"
                               "diameter_m,repose_angle_deg FROM warehouses "
                               "WHERE id=? AND active=1",
                               (warehouse_id,)).fetchone()
            if not row:
                conn.close()
                self._json(404, {"ok": False, "err": "warehouse not found"})
                return
            name = str(body.get("name", row[0])).strip()
            if not name or len(name) > 64:
                raise ValueError("仓库名称不能为空且不能超过 64 个字符")
            shape, length, width, height, diameter, repose = self._valid_warehouse_dimensions(
                body, dict(zip(("name", "shape_type", "length_m", "width_m",
                                "height_m", "diameter_m", "repose_angle_deg"), row)))
            if shape != row[1] and conn.execute(
                    "SELECT 1 FROM piles WHERE warehouse_id=? LIMIT 1",
                    (warehouse_id,)).fetchone():
                conn.close()
                self._json(409, {"ok": False, "err":
                    "仓库已有粮堆，不能直接切换仓型；请先完成粮堆几何重新配置"})
                return
            if shape == "flat" and conn.execute(
                    "SELECT 1 FROM piles WHERE warehouse_id=? AND "
                    "(x-l/2<0 OR z-w/2<0 OR x+l/2>? OR z+w/2>?) LIMIT 1",
                    (warehouse_id, length, width)).fetchone():
                conn.close()
                self._json(409, {"ok": False, "err":
                    "新仓房尺寸会使现有粮堆越界；请先调整粮堆位置或尺寸"})
                return
            conn.execute("UPDATE warehouses SET name=?,shape_type=?,length_m=?,width_m=?,"
                         "height_m=?,diameter_m=?,repose_angle_deg=? WHERE id=?",
                         (name, shape, length, width, height, diameter, repose, warehouse_id))
            for key, value in (("wh_l", length), ("wh_w", width), ("wh_h", height)):
                conn.execute("INSERT INTO warehouse_config(warehouse_id,key,value) "
                             "VALUES(?,?,?) ON CONFLICT(warehouse_id,key) DO UPDATE "
                             "SET value=excluded.value",
                             (warehouse_id, key, str(value)))
            if warehouse_id == 1:
                for key, value in (("wh_l", length), ("wh_w", width), ("wh_h", height)):
                    conn.execute("INSERT INTO config(key,value) VALUES(?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (key, str(value)))
            write_audit(conn, "warehouse.updated", "warehouse", warehouse_id,
                        {"name": name, "shape_type": shape,
                         "repose_angle_deg": repose}, warehouse_id=self.warehouse_id)
            conn.commit()
        except (ValueError, TypeError) as error:
            try:
                conn.close()
            except UnboundLocalError:
                pass
            self._json(400, {"ok": False, "err": str(error)})
            return
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            self._json(409, {"ok": False, "err": "仓库名称已存在"})
            return
        conn.close()
        self._json(200, {"ok": True, "id": warehouse_id, "name": name,
                         "shape_type": shape, "repose_angle_deg": repose})

    def _poles(self):
        conn = db()
        now = int(time.time())
        cfg = get_cfg(conn, self.warehouse_id)
        rows = conn.execute(
            "SELECT p.uid,p.name,p.x,p.y,p.z,p.spacing_m,p.last_ts,p.bat_mv,"
            "p.pile_id,pl.name FROM poles p LEFT JOIN piles pl ON pl.id=p.pile_id "
            "WHERE p.warehouse_id=? AND (p.deleted=0 OR p.deleted IS NULL)",
            (self.warehouse_id,)).fetchall()
        out = []
        for r in rows:
            last = r[6]
            snap = self._latest_snapshot(conn, r[0])
            thresholds = effective_thresholds(conn, uid=r[0])
            intervals = effective_intervals(conn, uid=r[0])
            remote_settings = _device_settings_state(conn, r[0])
            out.append({
                "uid": r[0], "name": r[1], "x": r[2], "y": r[3], "z": r[4],
                "spacing_m": r[5],
                "status": 1 if last and (now - last) < OFFLINE_SEC else 0,
                "last_ts": last,
                "bat_mv": r[7], "bat_pct": bat_pct(r[7]),
                "pile_id": r[8], "pile_name": r[9],
                "threshold": thresholds["values"],
                "threshold_sources": thresholds["sources"],
                "intervals": intervals["values"],
                "interval_sources": intervals["sources"],
                "remote_settings": remote_settings,
                "snapshot": snap,
                "node_positions": self._node_positions(conn, r[0]),
                "node_states": self._node_states(r[0], snap, cfg["node_offline_sec"]),
            })
        conn.close()
        self._json(200, out)

    def _pole(self, p):
        uid = p.split("/")[4]
        conn = db()
        r = conn.execute(
            "SELECT p.uid,p.name,p.x,p.y,p.z,p.spacing_m,p.last_ts,p.pending_cmd,"
            "p.bat_mv,p.pile_id,pl.name," + ",".join(
                "p." + key for key in DEVICE_SETTING_KEYS) + " FROM poles p LEFT JOIN piles pl "
            "ON pl.id=p.pile_id WHERE p.uid=? "
            "AND p.warehouse_id=? AND (p.deleted=0 OR p.deleted IS NULL)",
            (uid, self.warehouse_id)).fetchone()
        if not r:
            conn.close()
            self._json(404, {"ok": False, "err": "not found"})
            return
        now = int(time.time())
        cfg = get_cfg(conn, self.warehouse_id)
        thresholds = effective_thresholds(conn, uid=uid)
        intervals = effective_intervals(conn, uid=uid)
        remote_settings = _device_settings_state(conn, uid)
        snap = self._latest_snapshot(conn, uid)
        node_positions = self._node_positions(conn, uid)
        conn.close()
        self._json(200, {
            "uid": r[0], "name": r[1], "x": r[2], "y": r[3], "z": r[4],
            "spacing_m": r[5],
            "status": 1 if r[6] and (now - r[6]) < OFFLINE_SEC else 0,
            "last_ts": r[6], "pending_cmd": r[7],
            "bat_mv": r[8], "bat_pct": bat_pct(r[8]),
            "pile_id": r[9], "pile_name": r[10],
            "threshold": thresholds["values"],
            "threshold_sources": thresholds["sources"],
            "threshold_overrides": dict(zip(THRESHOLD_KEYS, r[11:15])),
            "intervals": intervals["values"],
            "interval_sources": intervals["sources"],
            "interval_overrides": dict(zip(INTERVAL_KEYS, r[15:17])),
            "remote_settings": remote_settings,
            "reporting": (snap or {}).get("reporting"),
            "snapshot": snap,
            "node_positions": node_positions,
            "node_states": self._node_states(uid, snap, cfg["node_offline_sec"]),
        })

    def _pole_data(self, p, q):
        uid = p.split("/")[4]
        hours = int(q.split("hours=")[1].split("&")[0]) if "hours=" in q else 24
        since = int(time.time()) - hours * 3600
        conn = db()
        rows = conn.execute(
            "SELECT ts,mode,alarm,nodes_json FROM snapshots WHERE uid=? "
            "AND warehouse_id=? AND ts>=? ORDER BY ts",
            (uid, self.warehouse_id, since)).fetchall()
        conn.close()
        out = [{"ts": r[0], "mode": r[1], "alarm": r[2], "nodes": json.loads(r[3])}
               for r in rows]
        self._json(200, out)

    def _alerts(self):
        conn = db()
        measured = conn.execute(
            "SELECT id,uid,node_addr,ch,value,th,begin_ts,end_ts,active FROM alert_log "
            "WHERE warehouse_id=? ORDER BY id DESC LIMIT 200",
            (self.warehouse_id,)).fetchall()
        forecast = conn.execute(
            "SELECT id,uid,node_addr,ch,value,th,begin_ts,end_ts,active,forecast_hour "
            "FROM forecast_alert_log WHERE warehouse_id=? ORDER BY id DESC LIMIT 200",
            (self.warehouse_id,)).fetchall()
        conn.close()
        out = [{"id": r[0], "uid": r[1], "node_addr": r[2], "ch": r[3],
                "value": r[4], "th": r[5], "begin_ts": r[6], "end_ts": r[7],
                "active": r[8], "source": "measured", "forecast_hour": None}
               for r in measured]
        out.extend({"id": "forecast-%s" % r[0], "uid": r[1],
                    "node_addr": r[2],
                    "ch": (r[3][8:] if str(r[3]).startswith("weather_") else r[3]),
                    "value": r[4], "th": r[5],
                    "begin_ts": r[6], "end_ts": r[7], "active": r[8],
                    "source": ("weather_forecast" if str(r[3]).startswith("weather_")
                               else "forecast"), "forecast_hour": r[9]}
                   for r in forecast)
        out.sort(key=lambda row: row["begin_ts"], reverse=True)
        self._json(200, out[:200])

    # ---------- 数据库浏览（db.html 数据源） ----------
    def _db_view(self, q):
        """跨杆最新快照浏览。查询参数: uid=探杆筛选  limit=条数(默认500,上限5000)。
        响应: {summary:{total, by_pole:[{uid,name,pile_name,count}], alerts_active},
               rows:[{ts,uid,name,pile_id,pile_name,mode,alarm,bat_mv,nodes:[...]}]}"""
        args = urllib.parse.parse_qs(q, keep_blank_values=True)
        uid = (args.get("uid") or [""])[0] or None
        try:
            limit = int((args.get("limit") or ["500"])[0])
        except (ValueError, TypeError):
            limit = 500
        limit = max(10, min(limit, 5000))
        try:
            hours = int((args.get("hours") or [""])[0])
            since = int(time.time()) - max(1, min(hours, 24 * 365)) * 3600
        except (ValueError, TypeError):
            since = None
        try:
            pile_id = int((args.get("pile_id") or [""])[0])
            if pile_id <= 0:
                pile_id = None
        except (ValueError, TypeError):
            pile_id = None
        conn = db()
        names = dict(conn.execute("SELECT uid,name FROM poles WHERE warehouse_id=? "
                                  "AND (deleted=0 OR deleted IS NULL)",
                                  (self.warehouse_id,)).fetchall())
        piles_n = dict(conn.execute("SELECT id,name FROM piles WHERE warehouse_id=?",
                                    (self.warehouse_id,)).fetchall())
        pile_of = dict(conn.execute(
            "SELECT uid,pile_id FROM poles WHERE pile_id IS NOT NULL "
            "AND warehouse_id=? AND (deleted=0 OR deleted IS NULL)",
            (self.warehouse_id,)).fetchall())
        filters, values = ["warehouse_id=?"], [self.warehouse_id]
        if uid:
            filters.append("uid=?")
            values.append(uid)
        if pile_id is not None:
            filters.append("uid IN (SELECT uid FROM poles WHERE pile_id=? "
                           "AND warehouse_id=? "
                           "AND (deleted=0 OR deleted IS NULL))")
            values.extend((pile_id, self.warehouse_id))
        if since is not None:
            filters.append("ts>=?")
            values.append(since)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        rows = conn.execute(
            "SELECT uid,ts,mode,alarm,nodes_json,bat_mv,reporting_json FROM snapshots" +
            where + " ORDER BY id DESC LIMIT ?", values + [limit]).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM snapshots WHERE warehouse_id=?",
                             (self.warehouse_id,)).fetchone()[0]
        cnt_rows = conn.execute(
            "SELECT uid,COUNT(*) FROM snapshots WHERE warehouse_id=? AND uid IN "
            "(SELECT uid FROM poles WHERE warehouse_id=? AND "
            "(deleted=0 OR deleted IS NULL)) GROUP BY uid ORDER BY COUNT(*) DESC",
            (self.warehouse_id, self.warehouse_id)).fetchall()
        acts = conn.execute("SELECT COUNT(*) FROM alert_log WHERE warehouse_id=? "
                             "AND active=1", (self.warehouse_id,)).fetchone()[0]
        conn.close()
        out = [{
            "ts": r[1], "uid": r[0], "name": names.get(r[0], r[0]),
            "pile_id": pile_of.get(r[0]), "pile_name": piles_n.get(pile_of.get(r[0])),
            "mode": r[2], "alarm": r[3], "nodes": json.loads(r[4]), "bat_mv": r[5],
            "reporting": json.loads(r[6] or "{}"),
        } for r in rows]
        by_pole = [{"uid": u, "name": names.get(u, u),
                    "pile_name": piles_n.get(pile_of.get(u)), "count": c}
                   for u, c in cnt_rows]
        self._json(200, {"summary": {"total": total, "by_pole": by_pole,
                                     "alerts_active": acts}, "rows": out})

    def _db_export(self, query, xlsx=False):
        """Export measured node, warehouse-environment, or model-weather history."""
        args = urllib.parse.parse_qs(query, keep_blank_values=True)
        kind = (args.get("kind") or [""])[0]
        if kind not in ("nodes", "env", "weather"):
            self._json(400, {"ok": False, "err": "kind must be nodes, env, or weather"})
            return
        try:
            hours = int((args.get("hours") or ["720"])[0])
        except (ValueError, TypeError):
            hours = 720
        since = int(time.time()) - max(1, min(hours, 24 * 365)) * 3600
        conn = db()
        rows = []
        if kind == "nodes":
            uid = (args.get("uid") or [""])[0] or None
            try:
                pile_id = int((args.get("pile_id") or [""])[0])
                if pile_id <= 0:
                    pile_id = None
            except (ValueError, TypeError):
                pile_id = None
            try:
                limit = max(1, min(int((args.get("limit") or ["300000"])[0]), 300000))
            except (ValueError, TypeError):
                limit = 300000
            filters, values = ["warehouse_id=?", "ts>=?"], [self.warehouse_id, since]
            if uid:
                filters.append("uid=?")
                values.append(uid)
            if pile_id is not None:
                filters.append("uid IN (SELECT uid FROM poles WHERE pile_id=? "
                               "AND warehouse_id=? "
                               "AND (deleted=0 OR deleted IS NULL))")
                values.extend((pile_id, self.warehouse_id))
            snapshots = conn.execute(
                "SELECT uid,ts,mode,alarm,nodes_json,bat_mv,reporting_json "
                "FROM snapshots WHERE " + " AND ".join(filters) +
                " ORDER BY ts,id LIMIT ?", values + [limit]).fetchall()
            names = dict(conn.execute("SELECT uid,name FROM poles WHERE warehouse_id=?",
                                      (self.warehouse_id,)).fetchall())
            pile_names = dict(conn.execute("SELECT id,name FROM piles WHERE warehouse_id=?",
                                           (self.warehouse_id,)).fetchall())
            pile_by_uid = dict(conn.execute("SELECT uid,pile_id FROM poles WHERE warehouse_id=?",
                                            (self.warehouse_id,)).fetchall())
            header = ["timestamp_utc", "probe_uid", "probe_name", "pile_name",
                      "node_address", "temperature_c", "relative_humidity_pct",
                      "node_status", "battery_mv", "mode", "alarm",
                      "sample_interval_ms", "report_interval_ms"]
            for row in snapshots:
                reporting = json.loads(row[6] or "{}")
                for node in json.loads(row[4] or "[]"):
                    rows.append([datetime.fromtimestamp(row[1], timezone.utc).isoformat(),
                                 row[0], names.get(row[0], row[0]),
                                 pile_names.get(pile_by_uid.get(row[0])),
                                 node.get("addr"), node.get("temp"), node.get("rh"),
                                 node.get("status"), row[5], row[2], row[3],
                                 reporting.get("sample_interval_ms"),
                                 reporting.get("report_interval_ms")])
        elif kind == "env":
            records = conn.execute(
                "SELECT id,ts,data_json FROM env WHERE warehouse_id=? AND ts>=? "
                "ORDER BY ts,id LIMIT 300000", (self.warehouse_id, since)).fetchall()
            decoded = [(record_id, ts, json.loads(data)) for record_id, ts, data in records]
            keys = sorted({key for _, _, data in decoded for key in data})
            header = ["timestamp_utc", "record_id"] + keys
            for record_id, ts, data in decoded:
                rows.append([datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                             record_id] + [data.get(key) for key in keys])
        else:
            records = conn.execute(
                "SELECT lat_key,lon_key,fetched_ts,current_json,series_json,source "
                "FROM warehouse_weather_runs WHERE warehouse_id=? AND run_slot>=? "
                "ORDER BY run_slot LIMIT 10000",
                (self.warehouse_id, since // 3600)).fetchall()
            header = ["record_type", "source", "latitude", "longitude", "fetched_utc",
                      "valid_utc", "temperature_c", "relative_humidity_pct", "weather_code",
                      "temperature_min_c", "temperature_max_c",
                      "precipitation_probability_max_pct", "precipitation_sum_mm",
                      "dew_point_c"]
            for lat, lon, fetched, current_json, series_json, source in records:
                current, daily_series, _timezone = _weather_current_bundle(current_json)
                if current:
                    rows.append(["current", source, float(lat), float(lon),
                                 datetime.fromtimestamp(fetched, timezone.utc).isoformat(),
                                 datetime.fromtimestamp(current["ts"], timezone.utc).isoformat()
                                 if current.get("ts") else None,
                                 current.get("temperature_c"), current.get("rh_pct"),
                                 current.get("weather_code"), None, None, None, None, None])
                for point in json.loads(series_json or "[]"):
                    rows.append(["forecast", source, float(lat), float(lon),
                                 datetime.fromtimestamp(fetched, timezone.utc).isoformat(),
                                 datetime.fromtimestamp(point["ts"], timezone.utc).isoformat()
                                 if point.get("ts") else None,
                                 point.get("temperature_c"), point.get("rh_pct"), None,
                                 None, None, None, None, None])
                for point in daily_series:
                    rows.append(["daily_forecast", source, float(lat), float(lon),
                                 datetime.fromtimestamp(fetched, timezone.utc).isoformat(),
                                 point.get("date"), None, None, point.get("weather_code"),
                                 point.get("temperature_min_c"),
                                 point.get("temperature_max_c"),
                                 point.get("precipitation_probability_max_pct"),
                                 point.get("precipitation_sum_mm"), None])
            observations = conn.execute("""SELECT lat_key,lon_key,weather_ts,
                temperature_c,rh_pct,dew_point_c,source,fetched_ts
                FROM warehouse_weather_observations
                WHERE warehouse_id=? AND weather_ts>=?
                ORDER BY weather_ts LIMIT 300000""",
                (self.warehouse_id, since)).fetchall()
            for lat, lon, valid_ts, temperature, humidity, dew_point, source, fetched in observations:
                rows.append(["historical_model", source, float(lat), float(lon),
                             datetime.fromtimestamp(fetched, timezone.utc).isoformat(),
                             datetime.fromtimestamp(valid_ts, timezone.utc).isoformat(),
                             temperature, humidity, None, None, None, None, None,
                             dew_point])
        conn.close()
        if xlsx and len(rows) > 1_048_575:
            self._json(413, {"ok": False,
                             "err": "导出记录超过单张 Excel 工作表上限，请缩小时间范围"})
            return
        suffix = "xlsx" if xlsx else "csv"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = "grainsilo_%s_%s.%s" % (kind, timestamp, suffix)
        if xlsx:
            payload = _xlsx_export(header, rows, kind, hours, query, timestamp)
        else:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(header)
            for row in rows:
                # Prevent spreadsheet formula execution for user-controlled text fields.
                writer.writerow([("'" + value if isinstance(value, str) and
                                  value.startswith(("=", "+", "-", "@", "\t")) else value)
                                 for value in row])
            payload = stream.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                         if xlsx else "text/csv; charset=utf-8-sig")
        self.send_header("Content-Disposition", "attachment; filename=\"%s\"" % filename)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---------- 粮堆（piles）管理 ----------
    def _pile_relayout(self):
        """仓尺寸变更后：全部粮堆按 id 顺序重新自动布局（互不重叠）。
        任一堆找不到空位则整体回滚，不修改数据库。"""
        conn = db()
        cfg = get_cfg(conn, self.warehouse_id)
        rows = conn.execute("SELECT id,l,w FROM piles WHERE warehouse_id=? ORDER BY id",
                            (self.warehouse_id,)).fetchall()
        placed, plan = [], []
        for pid, l, w in rows:
            pos = _place_pile(cfg, placed, l, w)
            if pos is None:
                conn.close()
                print(f"[PILES] relayout 失败：粮堆 id={pid} 无空位，已回滚")
                self._json(409, {"ok": False, "err":
                    "仓库空间不足：粮堆 id=%d 无空位，请增大仓库或减小粮堆" % pid})
                return
            plan.append((pos[0], pos[1], pid))
            placed.append({"x": pos[0], "z": pos[1], "l": l, "w": w})
        for x, z, pid in plan:
            conn.execute("UPDATE piles SET x=?, z=? WHERE id=? AND warehouse_id=?",
                         (x, z, pid, self.warehouse_id))
        conn.commit()
        conn.close()
        print(f"[PILES] 全部粮堆重新布局完成 {len(rows)} 堆")
        self._json(200, {"ok": True, "relocated": len(rows)})

    def _piles(self):
        """粮堆列表：体积/高度派生值 + 绑定的探杆数/探杆数组"""
        conn = db()
        cfg = get_cfg(conn, self.warehouse_id)
        warehouse = conn.execute("SELECT shape_type,length_m,width_m,height_m,"
                                 "diameter_m,repose_angle_deg FROM warehouses WHERE id=?",
                                 (self.warehouse_id,)).fetchone()
        rows = conn.execute("SELECT id,name,weight_kg,density,x,z,l,w,created_ts,"
                            "updated_ts FROM piles WHERE warehouse_id=? ORDER BY id",
                            (self.warehouse_id,)).fetchall()
        poles = conn.execute("SELECT uid,name,pile_id FROM poles "
                             "WHERE warehouse_id=? AND pile_id IS NOT NULL "
                             "AND (deleted=0 OR deleted IS NULL) "
                             "ORDER BY pile_id", (self.warehouse_id,)).fetchall()
        by_pile = {}
        for u, n, pid in poles:
            by_pile.setdefault(pid, []).append({"uid": u, "name": n})
        out = []
        for r in rows:
            shape_type = warehouse[0] if warehouse else "flat"
            repose = warehouse[5] if warehouse else 30.0
            diameter = warehouse[4] if warehouse else min(r[6], r[7])
            x, z, length, width = r[4], r[5], r[6], r[7]
            if shape_type == "silo":
                x = z = diameter / 2
                length = width = diameter
            heap = heap_geom(cfg, {"weight_kg": r[2], "density": r[3], "x": x,
                                   "z": z, "l": length, "w": width,
                                   "shape_type": shape_type,
                                   "warehouse_diameter_m": diameter,
                                   "repose_angle_deg": repose})
            threshold_state = effective_thresholds(conn, pile_id=r[0])
            interval_state = effective_intervals(conn, pile_id=r[0])
            out.append({
                "id": r[0], "name": r[1], "weight_kg": r[2], "density": r[3],
                "x": x, "z": z, "l": length, "w": width,
                "created_ts": r[8], "updated_ts": r[9],
                "volume": round(heap["volume"], 1) if heap else 0.0,
                "height": round(heap["h"], 2) if heap else 0.0,
                "warehouse_shape_type": warehouse[0] if warehouse else "flat",
                "warehouse_dimensions": ({
                    "length_m": warehouse[1], "width_m": warehouse[2],
                    "height_m": warehouse[3], "diameter_m": warehouse[4],
                    "repose_angle_deg": warehouse[5],
                } if warehouse else None),
                "volume_source": "weight_divided_by_density_estimate",
                "geometry_status": heap["geometry_status"] if heap else "no_inventory_volume",
                "geometry_source": "parameterized_estimate" if heap else "unavailable",
                "shape_type": shape_type,
                "base_radius_m": heap.get("base_radius_m") if heap else None,
                "top_radius_m": heap.get("top_radius_m") if heap else None,
                "repose_angle_deg": repose,
                "pole_count": len(by_pile.get(r[0], [])),
                "poles": by_pile.get(r[0], []),
                "thresholds": threshold_state["values"],
                "threshold_sources": threshold_state["sources"],
                "threshold_overrides": dict(zip(THRESHOLD_KEYS, conn.execute(
                    "SELECT " + ",".join(THRESHOLD_KEYS) +
                    " FROM piles WHERE id=?", (r[0],)).fetchone())),
                "intervals": interval_state["values"],
                "interval_sources": interval_state["sources"],
                "interval_overrides": dict(zip(INTERVAL_KEYS, conn.execute(
                    "SELECT " + ",".join(INTERVAL_KEYS) +
                    " FROM piles WHERE id=?", (r[0],)).fetchone())),
            })
        conn.close()
        self._json(200, out)

    def _crop_emc_post(self):
        try:
            body = self._read_bounded_json()
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            result = calculate_emc(
                body.get("crop_type"), body.get("temp_c"), body.get("rh_pct"),
                wheat_type=body.get("wheat_type", "soft"),
                paddy_path=body.get("paddy_path", "both"))
        except (ValueError, TypeError) as error:
            self._json(400, {"ok": False, "err": str(error)})
            return
        result["ok"] = True
        self._json(200, result)

    def _crop_profile_get(self, query):
        try:
            raw_id = urllib.parse.parse_qs(query).get("pile_id", [""])[0]
            pile_id = int(raw_id)
            if pile_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "err": "invalid pile_id"})
            return
        conn = db()
        if not conn.execute("SELECT 1 FROM piles WHERE id=? AND warehouse_id=?",
                            (pile_id, self.warehouse_id)).fetchone():
            conn.close()
            self._json(404, {"ok": False, "err": "pile not found"})
            return
        row = conn.execute("SELECT crop_type,wheat_type,paddy_path FROM crop_profiles "
                           "WHERE pile_id=?", (pile_id,)).fetchone()
        conn.close()
        profile = None if row is None else {
            "pile_id": pile_id, "crop_type": row[0],
            "wheat_type": row[1], "paddy_path": row[2]}
        self._json(200, {"ok": True, "profile": profile})

    def _crop_profile_post(self):
        try:
            body = self._read_bounded_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        pile_id = body.get("pile_id") if isinstance(body, dict) else None
        crop_type = body.get("crop_type") if isinstance(body, dict) else None
        wheat_type = body.get("wheat_type", "soft") if isinstance(body, dict) else None
        paddy_path = body.get("paddy_path", "both") if isinstance(body, dict) else None
        if (not isinstance(pile_id, int) or isinstance(pile_id, bool) or pile_id <= 0
                or crop_type not in ("paddy", "wheat", "corn")
                or wheat_type not in ("soft", "hard")
                or paddy_path not in ("both", "adsorption", "desorption")):
            self._json(400, {"ok": False, "err": "invalid crop profile"})
            return
        conn = db()
        if not conn.execute("SELECT 1 FROM piles WHERE id=? AND warehouse_id=?",
                            (pile_id, self.warehouse_id)).fetchone():
            conn.close()
            self._json(404, {"ok": False, "err": "pile not found"})
            return
        updated_ts = int(time.time())
        conn.execute("""INSERT INTO crop_profiles(pile_id,crop_type,wheat_type,
                      paddy_path,updated_ts) VALUES(?,?,?,?,?)
                      ON CONFLICT(pile_id) DO UPDATE SET crop_type=excluded.crop_type,
                      wheat_type=excluded.wheat_type,paddy_path=excluded.paddy_path,
                      updated_ts=excluded.updated_ts""",
                     (pile_id, crop_type, wheat_type, paddy_path, updated_ts))
        write_audit(conn, "crop_profile_updated", "pile", pile_id,
                    {"crop_type": crop_type, "wheat_type": wheat_type,
                     "paddy_path": paddy_path}, warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        self._json(200, {"ok": True, "pile_id": pile_id,
                         "updated_ts": updated_ts})

    def _pile_create(self):
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "err": "expect object"})
            return
        conn = db()
        now = int(time.time())
        uids = body.get("pole_uids", [])
        if not isinstance(uids, list):
            conn.close()
            self._json(400, {"ok": False, "err": "pole_uids must be an array"})
            return
        uids = list(dict.fromkeys(str(uid) for uid in uids))
        if uids:
            marks = ",".join("?" for _ in uids)
            found = {row[0] for row in conn.execute(
                "SELECT uid FROM poles WHERE warehouse_id=? AND "
                "(deleted=0 OR deleted IS NULL) AND uid IN (" + marks + ")",
                [self.warehouse_id] + uids).fetchall()}
            if found != set(uids):
                conn.close()
                self._json(404, {"ok": False, "err":
                    "一个或多个探杆不存在或不属于当前仓库；没有创建粮堆"})
                return
        warehouse = conn.execute("SELECT shape_type,length_m,width_m,diameter_m "
                                 "FROM warehouses WHERE id=?",
                                 (self.warehouse_id,)).fetchone()
        shape_type = warehouse[0] if warehouse else "flat"
        if shape_type == "silo":
            if conn.execute("SELECT 1 FROM piles WHERE warehouse_id=? LIMIT 1",
                            (self.warehouse_id,)).fetchone():
                conn.close()
                self._json(409, {"ok": False, "err":
                    "一个圆筒仓档案对应一个粮堆；多个独立筒仓请分别建立仓库档案"})
                return
            l = w = float(warehouse[3])
        else:
            try:
                l, w = float(body.get("l", 20)), float(body.get("w", 10))
            except (TypeError, ValueError):
                conn.close()
                self._json(400, {"ok": False, "err": "粮堆平面尺寸必须为数值"})
                return
            if not all(math.isfinite(value) and value >= 0.5 for value in (l, w)):
                conn.close()
                self._json(400, {"ok": False, "err": "粮堆平面尺寸须为至少 0.5 米的有限数值"})
                return
        # 自动布局：在仓库内找不与现有粮堆重叠的空位（x/z 由算法决定，防堆挤堆）
        cfg = get_cfg(conn, self.warehouse_id)
        exist = [{"x": r[0], "z": r[1], "l": r[2], "w": r[3]}
                 for r in conn.execute(
                     "SELECT x,z,l,w FROM piles WHERE warehouse_id=? ORDER BY id",
                     (self.warehouse_id,)).fetchall()]
        pos = ((l / 2, w / 2) if shape_type == "silo" else
               _place_pile(cfg, exist, l, w))
        if pos is None:
            conn.close()
            print(f"[PILES] 拒绝创建：仓库空间不足（l={l} w={w}）")
            self._json(409, {"ok": False, "err":
                "仓库空间不足：无法放置 %.1f×%.1f m 的粮堆"
                "（与现有粮堆重叠），请增大仓库或减小粮堆" % (l, w)})
            return
        x, z = pos
        cur = conn.execute(
            "INSERT INTO piles(name,weight_kg,density,x,z,l,w,created_ts,updated_ts,"
            "warehouse_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (body.get("name", "新粮堆"), float(body.get("weight_kg", 0)),
             float(body.get("density", 750)), x, z, l, w, now, now,
             self.warehouse_id))
        pid = cur.lastrowid
        if uids:
            for u in uids:
                conn.execute("UPDATE poles SET pile_id=? WHERE uid=? AND warehouse_id=? "
                             "AND (deleted=0 OR deleted IS NULL)",
                             (pid, u, self.warehouse_id))
                _sync_device_settings(conn, u)
        conn.commit()
        conn.close()
        print(f"[PILES] 新建粮堆 id={pid} {body.get('name')} "
              f"{body.get('weight_kg')}kg 自动放置@({x},{z}) "
              f"绑定探杆 {len(uids)} 根")
        self._json(200, {"ok": True, "id": pid})

    def _pile_update(self, p):
        try:
            pid = int(p.split("/")[4])
        except (IndexError, ValueError):
            self._json(400, {"ok": False, "err": "invalid pile id"})
            return
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "err": "expect object"})
            return
        conn = db()
        if not conn.execute("SELECT 1 FROM piles WHERE id=? AND warehouse_id=?",
                            (pid, self.warehouse_id)).fetchone():
            conn.close()
            self._json(404, {"ok": False, "err": "pile not found"})
            return
        binding_uids = None
        if "pole_uids" in body:
            if not isinstance(body["pole_uids"], list):
                conn.close()
                self._json(400, {"ok": False, "err": "pole_uids must be an array"})
                return
            binding_uids = list(dict.fromkeys(str(uid) for uid in body["pole_uids"]))
            if binding_uids:
                marks = ",".join("?" for _ in binding_uids)
                found = {row[0] for row in conn.execute(
                    "SELECT uid FROM poles WHERE warehouse_id=? AND "
                    "(deleted=0 OR deleted IS NULL) AND uid IN (" + marks + ")",
                    [self.warehouse_id] + binding_uids).fetchall()}
                if found != set(binding_uids):
                    conn.close()
                    self._json(404, {"ok": False, "err":
                        "一个或多个探杆不存在或不属于当前仓库；未更改粮堆绑定"})
                    return
        try:
            threshold_updates = _threshold_updates(body)
            interval_updates = _interval_updates(body)
            if threshold_updates or interval_updates:
                _validate_all_device_settings(
                    conn, pile_update=(pid, {**threshold_updates,
                                             **interval_updates}),
                    warehouse_id=self.warehouse_id)
        except ValueError as error:
            conn.close()
            self._json(400, {"ok": False, "err": str(error)})
            return
        sets, vals = [], []
        for f in ("name", "weight_kg", "density", "x", "z", "l", "w"):
            if f in body:
                if f in ("x", "z", "l", "w") and conn.execute(
                        "SELECT shape_type FROM warehouses WHERE id=?",
                        (self.warehouse_id,)).fetchone()[0] == "silo":
                    continue
                sets.append(f"{f}=?")
                vals.append(body[f])
        for key, value in threshold_updates.items():
            sets.append(f"{key}=?")
            vals.append(value)
        for key, value in interval_updates.items():
            sets.append(f"{key}=?")
            vals.append(value)
        if sets:
            sets.append("updated_ts=?")
            vals.append(int(time.time()))
            conn.execute(f"UPDATE piles SET {','.join(sets)} WHERE id=? AND warehouse_id=?",
                         vals + [pid, self.warehouse_id])
        if binding_uids is not None:
            conn.execute("UPDATE poles SET pile_id=NULL WHERE pile_id=? AND warehouse_id=?",
                         (pid, self.warehouse_id))
            for u in binding_uids:
                conn.execute("UPDATE poles SET pile_id=? WHERE uid=? AND warehouse_id=? "
                             "AND (deleted=0 OR deleted IS NULL)",
                             (pid, u, self.warehouse_id))
        if threshold_updates or interval_updates or "pole_uids" in body:
            affected = conn.execute(
                "SELECT uid FROM poles WHERE pile_id=? AND warehouse_id=? "
                "AND (deleted=0 OR deleted IS NULL)", (pid, self.warehouse_id)).fetchall()
            for (uid,) in affected:
                _sync_device_settings(conn, uid)
        conn.commit()
        conn.close()
        print(f"[PILES] 更新粮堆 id={pid} {body}")
        self._json(200, {"ok": True})

    def _config_get(self):
        conn = db()
        cfg = get_cfg(conn, self.warehouse_id)
        # 默认粮堆 = 第一堆（旧版全局粮重已迁移为 1 号粮堆）
        pile = None
        r = conn.execute("SELECT weight_kg,density,x,z,l,w FROM piles "
                         "WHERE warehouse_id=? ORDER BY id LIMIT 1",
                         (self.warehouse_id,)).fetchone()
        warehouse = conn.execute("SELECT id,name,shape_type,length_m,width_m,"
                                  "height_m,diameter_m,repose_angle_deg FROM warehouses WHERE id=?",
                                  (self.warehouse_id,)).fetchone()
        if r:
            pile = {"weight_kg": r[0], "density": r[1], "x": r[2], "z": r[3],
                    "l": r[4], "w": r[5]}
        heap = heap_geom(cfg, pile) if pile else None
        conn.close()
        out = {k: cfg[k] for k in CONFIG_KEYS}
        if warehouse:
            out["warehouse"] = {"id": warehouse[0], "name": warehouse[1],
                                "shape_type": warehouse[2], "length_m": warehouse[3],
                                "width_m": warehouse[4], "height_m": warehouse[5],
                                "diameter_m": warehouse[6],
                                "repose_angle_deg": warehouse[7]}
        if heap:
            out["grain_volume"] = round(heap["volume"], 1)   # m³
            out["grain_height"] = round(heap["h"], 2)        # m
        else:
            out["grain_volume"] = 0.0
            out["grain_height"] = 0.0
        self._json(200, out)

    def _health(self):
        """Compact operational health summary for the workbench and diagnostics."""
        now = int(time.time())
        conn = db()
        version_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        poles = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN last_ts>? THEN 1 ELSE 0 END) "
            "FROM poles WHERE warehouse_id=? AND (deleted=0 OR deleted IS NULL)",
            (now - OFFLINE_SEC, self.warehouse_id)
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM alert_log WHERE warehouse_id=? AND active=1",
            (self.warehouse_id,)).fetchone()[0]
        env = conn.execute("SELECT ts FROM env WHERE warehouse_id=? "
                            "ORDER BY id DESC LIMIT 1",
                            (self.warehouse_id,)).fetchone()
        registry = conn.execute("SELECT COUNT(*) FROM environment_registry "
                                "WHERE enabled=1").fetchone()[0]
        conn.close()
        self._json(200, {
            "ok": True, "ts": now,
            "schema_version": int(version_row[0]) if version_row else 0,
            "database": {"path": DB, "writable": os.access(DB, os.W_OK)},
            "poles": {"total": int(poles[0] or 0), "online": int(poles[1] or 0)},
            "active_alerts": int(active),
            "environment": {"registry_enabled": int(registry),
                             "last_ts": env[0] if env else None},
        })

    def _actuator_capabilities(self):
        """Read-only reservation; this build has no actuator control endpoint."""
        self._json(200, {"ok": True, "control_enabled": False, "data": [
            {"key": key, "label": label, "connected": False,
             "controllable": False, "state": "not_connected",
             "control_endpoint": None}
            for key, label in ACTUATOR_CAPABILITIES
        ]})

    def _env_registry(self):
        conn = db()
        rows = conn.execute("SELECT key,label,unit,enabled,source,updated_ts "
                            "FROM environment_registry ORDER BY key").fetchall()
        conn.close()
        self._json(200, {"ok": True, "data": [
            {"key": r[0], "label": r[1], "unit": r[2], "enabled": bool(r[3]),
             "source": r[4], "updated_ts": r[5]} for r in rows
        ]})

    def _audit(self, q):
        limit = 100
        if "limit=" in q:
            try:
                limit = int(q.split("limit=")[1].split("&")[0])
            except ValueError:
                pass
        limit = max(1, min(limit, 500))
        conn = db()
        rows = conn.execute("SELECT id,ts,actor,action,entity,entity_id,detail_json "
                            "FROM audit_log WHERE warehouse_id=? "
                            "ORDER BY id DESC LIMIT ?",
                            (self.warehouse_id, limit)).fetchall()
        conn.close()
        self._json(200, {"ok": True, "data": [
            {"id": r[0], "ts": r[1], "actor": r[2], "action": r[3],
             "entity": r[4], "entity_id": r[5], "detail": json.loads(r[6])}
            for r in rows
        ]})

    def _env_latest(self):
        conn = db()
        r = conn.execute("SELECT id,ts,data_json FROM env WHERE warehouse_id=? "
                         "ORDER BY id DESC LIMIT 1", (self.warehouse_id,)).fetchone()
        conn.close()
        if not r:
            self._json(200, {"ok": True, "data": None, "empty": True,
                             "err": "no env data"})
            return
        self._json(200, {"id": r[0], "ts": r[1], "data": json.loads(r[2])})

    def _weather_location_get(self):
        conn = db()
        row = conn.execute("SELECT lat,lon,updated_ts FROM warehouse_weather "
                           "WHERE warehouse_id=?", (self.warehouse_id,)).fetchone()
        conn.close()
        self._json(200, {"ok": True, "configured": bool(row),
                         "lat": row[0] if row else None,
                         "lon": row[1] if row else None,
                         "updated_ts": row[2] if row else None})

    def _weather_location_post(self):
        try:
            body = self._read_bounded_json(1024)
            lat, lon = float(body.get("lat")), float(body.get("lon"))
            if (not math.isfinite(lat) or not math.isfinite(lon) or
                    not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0):
                raise ValueError("坐标超出范围")
        except (ValueError, TypeError, AttributeError) as error:
            self._json(400, {"ok": False, "err": str(error) or "纬度/经度无效"})
            return
        lat, lon = round(lat, 6), round(lon, 6)
        now = int(time.time())
        conn = db()
        conn.execute("""INSERT INTO warehouse_weather(warehouse_id,lat,lon,updated_ts)
            VALUES(?,?,?,?) ON CONFLICT(warehouse_id) DO UPDATE SET lat=excluded.lat,
            lon=excluded.lon,updated_ts=excluded.updated_ts""",
                     (self.warehouse_id, lat, lon, now))
        if self.warehouse_id == 1:
            conn.execute("""INSERT INTO weather_location(id,lat,lon,updated_ts)
                VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET lat=excluded.lat,
                lon=excluded.lon,updated_ts=excluded.updated_ts""", (lat, lon, now))
        write_audit(conn, "weather.location.update", "weather_location",
                    str(self.warehouse_id), {"lat": lat, "lon": lon},
                    warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        self._json(200, {"ok": True, "lat": lat, "lon": lon,
                         "updated_ts": now})

    def _weather_geocode(self, query):
        name = (urllib.parse.parse_qs(query, keep_blank_values=True).get("q") or [""])[0]
        if len(str(name).strip()) < 2 or len(str(name)) > 120:
            self._json(400, {"ok": False, "err": "请输入 2–120 个字符的城市或地区名称"})
            return
        try:
            results = _geocode_locations(name)
        except Exception as exc:
            self._json(502, {"ok": False, "err": "地点搜索暂不可用：%s" % exc})
            return
        self._json(200, {"ok": True, "results": results,
                         "source": "Open-Meteo Geocoding / GeoNames",
                         "saved": False})

    def _weather_backfill_post(self):
        try:
            body = self._read_bounded_json(2048)
            start_text = str(body.get("start_date") or "")
            end_text = str(body.get("end_date") or "")
            start = datetime.strptime(start_text, "%Y-%m-%d").date()
            end = datetime.strptime(end_text, "%Y-%m-%d").date()
            if start.isoformat() != start_text or end.isoformat() != end_text:
                raise ValueError("日期必须使用 YYYY-MM-DD")
            if start > end or (end - start).days > 365:
                raise ValueError("历史天气范围最多 366 天，且开始日期不能晚于结束日期")
            if end > datetime.now(timezone.utc).date():
                raise ValueError("历史天气结束日期不能晚于今天（UTC）")
        except (ValueError, TypeError, AttributeError) as exc:
            self._json(400, {"ok": False, "err": str(exc) or "历史天气日期无效"})
            return

        conn = db()
        location = conn.execute(
            "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
            (self.warehouse_id,)).fetchone()
        conn.close()
        if not location:
            self._json(409, {"ok": False, "err": "请先为当前仓库保存天气坐标"})
            return
        lat, lon = round(float(location[0]), 6), round(float(location[1]), 6)
        try:
            records = fetch_historical_weather(lat, lon, start_text, end_text)
        except Exception as exc:
            self._json(502, {"ok": False, "err": "历史天气获取失败：%s" % exc})
            return

        conn = db()
        current_location = conn.execute(
            "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
            (self.warehouse_id,)).fetchone()
        if (not current_location or round(float(current_location[0]), 6) != lat or
                round(float(current_location[1]), 6) != lon):
            conn.close()
            self._json(409, {"ok": False,
                             "err": "回填期间仓库天气位置已更改，请重新确认并发起回填"})
            return
        fetched_ts = int(time.time())
        conn.executemany("""INSERT INTO warehouse_weather_observations
            (warehouse_id,lat_key,lon_key,weather_ts,temperature_c,rh_pct,
             dew_point_c,source,fetched_ts) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(warehouse_id,lat_key,lon_key,weather_ts) DO UPDATE SET
              temperature_c=excluded.temperature_c,rh_pct=excluded.rh_pct,
              dew_point_c=excluded.dew_point_c,source=excluded.source,
              fetched_ts=excluded.fetched_ts""",
                         [(self.warehouse_id, f"{lat:.6f}", f"{lon:.6f}",
                           int(row["ts"]), row.get("temperature_c"),
                           row.get("rh_pct"), row.get("dew_point_c"),
                           str(row.get("source") or "Open-Meteo Historical Forecast"),
                           fetched_ts) for row in records])
        write_audit(conn, "weather.history.backfill", "weather_history",
                    str(self.warehouse_id),
                    {"start_date": start_text, "end_date": end_text,
                     "record_count": len(records),
                     "source": "Open-Meteo Historical Forecast"},
                    warehouse_id=self.warehouse_id)
        conn.commit()
        conn.close()
        self._json(200, {"ok": True, "inserted": len(records),
                         "start_date": start_text, "end_date": end_text,
                         "source": "Open-Meteo Historical Forecast",
                         "note": "逐小时网格天气模型历史，不是仓内或本地气象站实测。"})

    def _weather_history(self, query):
        try:
            hours = int(urllib.parse.parse_qs(query).get("hours", ["168"])[0])
        except (ValueError, TypeError):
            hours = 168
        hours = max(1, min(hours, 24 * 365))
        since_slot = int(time.time()) // 3600 - hours
        conn = db()
        rows = conn.execute("""SELECT lat_key,lon_key,run_slot,fetched_ts,
            current_json,series_json,source FROM warehouse_weather_runs
            WHERE warehouse_id=? AND run_slot>=? ORDER BY run_slot DESC LIMIT 1000""",
                            (self.warehouse_id, since_slot)).fetchall()
        location = conn.execute(
            "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
            (self.warehouse_id,)).fetchone()
        observations = []
        if location:
            lat_key, lon_key = f"{float(location[0]):.6f}", f"{float(location[1]):.6f}"
            observation_rows = conn.execute("""SELECT weather_ts,temperature_c,rh_pct,
                dew_point_c,source,fetched_ts FROM warehouse_weather_observations
                WHERE warehouse_id=? AND lat_key=? AND lon_key=? AND weather_ts>=?
                ORDER BY weather_ts DESC LIMIT 10000""",
                (self.warehouse_id, lat_key, lon_key,
                 int(time.time()) - hours * 3600)).fetchall()
            observations = [{"ts": row[0], "temperature_c": row[1],
                             "rh_pct": row[2], "dew_point_c": row[3],
                             "source": row[4], "fetched_ts": row[5]}
                            for row in reversed(observation_rows)]
        conn.close()
        data = []
        for row in rows:
            current, daily_series, tz_name = _weather_current_bundle(row[4])
            data.append({
                "lat": float(row[0]), "lon": float(row[1]), "run_slot": row[2],
                "fetched_ts": row[3], "current": current,
                "daily_series": daily_series, "timezone": tz_name,
                "series": json.loads(row[5]), "source": row[6],
            })
        self._json(200, {"ok": True, "data": data,
                         "observations": observations,
                         "observations_source": "Open-Meteo Historical Forecast",
                         "observations_note": "逐小时网格天气模型历史，不是现场气象站实测。"})

    def _env_history(self, q):
        hours = int(q.split("hours=")[1].split("&")[0]) if "hours=" in q else 24
        since = int(time.time()) - hours * 3600
        conn = db()
        rows = conn.execute("SELECT id,ts,data_json FROM env WHERE warehouse_id=? "
                            "AND ts>=? ORDER BY ts",
                            (self.warehouse_id, since)).fetchall()
        conn.close()
        self._json(200, [{"id": r[0], "ts": r[1], "data": json.loads(r[2])}
                         for r in rows])

    def _heatmap(self, q):
        """按已校准节点坐标生成 IDW 热场；通信地址不代表几何位置。"""
        conn = db()
        cfg = get_cfg(conn, self.warehouse_id)
        conn.close()
        if "step" in q:
            try:
                cfg["heatmap_step"] = float(q.split("step=")[1].split("&")[0])
            except ValueError:
                pass
        pile_id = None
        if "pile=" in q:
            try:
                pile_id = int(q.split("pile=")[1].split("&")[0])
            except ValueError:
                pass
        heap, pts, samples = _idw_grid(cfg, pile_id, self.warehouse_id)
        probes = [{"uid": s.get("uid"), "node_uid": "%s-%s" % (s.get("uid"), s.get("addr")),
                   "name": s.get("name"), "addr": s.get("addr"),
                   "x": round(s["x"], 3), "y": round(s["y"], 3),
                   "z": round(s["z"], 3),
                   "depth_m": round(s["d"], 2) if s.get("d") is not None else None,
                   "temp": round(s["t"], 2),
                   "rh": round(s["h"], 2) if s.get("h") is not None else None,
                   "ts": s.get("ts"), "online": bool(s.get("online")),
                   "position_validated": True}
                  for s in samples]
        active_samples = [s for s in samples if s.get("online")]
        probe_uids = {s["uid"] for s in active_samples}
        xz = {(s["x"], s["z"]) for s in active_samples}
        points_xz = sorted(xz)
        non_collinear_area2 = 0.0
        if len(points_xz) >= 3:
            a, b = points_xz[0], points_xz[-1]
            non_collinear_area2 = max(
                abs((b[0] - a[0]) * (p[1] - a[1]) -
                    (b[1] - a[1]) * (p[0] - a[0])) for p in points_xz)
        vertical_span = (max(s["y"] for s in active_samples) -
                         min(s["y"] for s in active_samples)) if active_samples else 0.0
        volumetric = (len(probe_uids) >= 3 and len(xz) >= 3 and
                      non_collinear_area2 >= 1.0 and vertical_span >= 0.5)
        if not active_samples:
            support, confidence = "none", "none"
        elif len(active_samples) == 1:
            support, confidence = "single_point_flat", "very_low"
        elif len(probe_uids) == 1:
            support, confidence = "single_probe_profile", "low"
        elif volumetric:
            support, confidence = "multi_probe_idw", "limited"
        else:
            support, confidence = "sparse_multi_probe_idw", "low"
        field_meta = {"method": "idw", "interpolated": False,
                      "sample_count": len(probes), "coordinates_calibrated": bool(probes),
                      "spatially_validated": volumetric,
                      "spatial_support": support, "confidence": confidence}
        if heap is None or not pts:
            self._json(200, {"ok": True, "step": cfg["heatmap_step"],
                             "heap": heap, "points": [], "lines": [],
                             "probes": probes, "field": field_meta})
            return
        t0 = time.time()
        points = []
        for pt in pts:
            y = _heap_height(heap, pt[0], pt[1]) - pt[2]
            v = _idw((pt[0], y, pt[1]), active_samples)
            if v and v[0] is not None:
                points.append([round(pt[0], 2), round(pt[1], 2), round(pt[2], 2),
                               round(v[0], 2),
                               round(v[1], 2) if v[1] is not None else None])
        # Sparse telemetry does not justify synthetic CFD-like streamlines.
        lines = []
        print(f"[HEATMAP] 网格 {len(pts)} 插值 {len(points)} 点 "
              f"({(time.time()-t0)*1000:.0f}ms)")
        vals = [s["t"] for s in active_samples if s.get("t") is not None]
        field_meta.update({"interpolated": bool(points),
                           "measured_min": min(vals) if vals else None,
                           "measured_max": max(vals) if vals else None})
        self._json(200, {"ok": True, "step": cfg["heatmap_step"], "heap": heap,
                         "points": points, "lines": lines, "probes": probes,
                         "field": field_meta})

    def _probe_forecast(self, q):
        """Return separate S3, trend-only, and weather-assisted air forecasts."""
        args = urllib.parse.parse_qs(q, keep_blank_values=True)
        uid = (args.get("uid") or [""])[0].strip()
        try:
            addr = int((args.get("addr") or [""])[0])
        except (TypeError, ValueError):
            addr = 0
        if not uid or len(uid) > 32 or addr < 1 or addr > 247:
            self._json(400, {"ok": False, "err": "uid and addr=1..247 required"})
            return
        conn = db()
        pole = conn.execute("SELECT uid FROM poles WHERE uid=? AND warehouse_id=?",
                             (uid, self.warehouse_id)).fetchone()
        if not pole:
            conn.close()
            self._json(404, {"ok": False, "err": "unknown probe"})
            return
        now = int(time.time())
        samples = _probe_samples(conn, uid, addr)
        station_prediction = predict_air_state(samples, now_ts=now)
        weather_probe_samples = _probe_hourly_samples(
            conn, uid, addr, self.warehouse_id)
        location = conn.execute(
            "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
            (self.warehouse_id,)).fetchone()
        weather_history = []
        cached_weather = None
        if location:
            lat, lon = float(location[0]), float(location[1])
            weather_history_rows = conn.execute("""SELECT weather_ts,
                temperature_c,rh_pct FROM warehouse_weather_observations
                WHERE warehouse_id=? AND lat_key=? AND lon_key=? AND weather_ts>=?
                ORDER BY weather_ts DESC LIMIT 1080""",
                (self.warehouse_id, f"{lat:.6f}", f"{lon:.6f}", now - 45 * 86400)).fetchall()
            weather_history = [{"ts": row[0], "temperature_c": row[1],
                                "rh_pct": row[2]} for row in reversed(weather_history_rows)]
            cached_weather = _cached_weather_run(
                conn, lat, lon, now, max_age_sec=900,
                warehouse_id=self.warehouse_id)
        latest = conn.execute(
            "SELECT ts,nodes_json FROM snapshots WHERE uid=? AND warehouse_id=? "
            "ORDER BY ts DESC,id DESC LIMIT 1",
            (uid, self.warehouse_id)).fetchone()
        cfg = effective_thresholds(conn, uid=uid, warehouse_id=self.warehouse_id)["values"]
        conn.close()

        if location:
            lat, lon = float(location[0]), float(location[1])
            weather_forecast_bundle = cached_weather
            if weather_forecast_bundle is None:
                weather_forecast_bundle = _open_meteo(lat, lon, 48)
                if weather_forecast_bundle.get("status") == "ready":
                    archive_conn = db()
                    _store_weather_run(archive_conn, lat, lon,
                                       weather_forecast_bundle, now,
                                       warehouse_id=self.warehouse_id)
                    archive_conn.commit()
                    archive_conn.close()
            weather_prediction = predict_weather_assisted_air_state(
                weather_probe_samples, weather_history,
                weather_forecast_bundle.get("series", [])
                if weather_forecast_bundle and
                weather_forecast_bundle.get("status") == "ready" else [], now)
            if (weather_forecast_bundle and
                    weather_forecast_bundle.get("status") != "ready" and
                    weather_prediction.get("status") == "unavailable"):
                weather_prediction["weather_error"] = str(
                    weather_forecast_bundle.get("error") or "天气模型不可用")[:240]
        else:
            weather_prediction = {
                "status": "not_configured", "reason": "warehouse_weather_location_missing",
                "sample_count": 0, "required_samples": 72,
                "forecast": [],
                "note": "请先为当前仓库设置天气位置；此模型预测探杆周围空气，不预测粮粒含水率。",
            }

        latest_node = None
        if latest:
            try:
                latest_node = next((node for node in json.loads(latest[1])
                                    if int(node.get("addr", -1)) == addr), None)
            except (TypeError, ValueError, StopIteration):
                latest_node = None
        current = next((sample for sample in samples
                        if latest and int(sample["ts"]) == int(latest[0])), None)
        current = current or (samples[0] if samples else None)
        measured = []
        if current:
            for channel in ("temp", "rh"):
                value = current.get(channel)
                if value is None:
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                crossing = _forecast_crossing(
                    {"status": "ready", "forecast": [{
                        "hour": 0,
                        "temperature_c": value if channel == "temp" else None,
                        "rh_pct": value if channel == "rh" else None,
                    }]}, channel, cfg)
                # Current measurements use the same configured thresholds.
                if crossing:
                    crossing.update({"channel": channel, "source": "measured"})
                    measured.append(crossing)

        station_crossings = []
        for channel in ("temp", "rh"):
            crossing = _forecast_crossing(station_prediction, channel, cfg)
            if crossing:
                crossing.update({"channel": channel, "source": "station"})
                station_crossings.append(crossing)

        s3_prediction = latest_node.get("forecast") if latest_node else None
        if isinstance(s3_prediction, dict):
            s3_prediction = dict(s3_prediction)
            snapshot_age = now - int(latest[0]) if latest else -1
            if snapshot_age < 0 or snapshot_age >= OFFLINE_SEC:
                s3_prediction["points"] = []
                s3_prediction["status"] = "stale"
            else:
                qualified = []
                for point in s3_prediction.get("points", []):
                    if not isinstance(point, dict):
                        continue
                    try:
                        hour = int(point.get("hour"))
                        span = int(point.get("history_span_s"))
                        sample_count = int(point.get("sample_count"))
                    except (TypeError, ValueError):
                        continue
                    required = max(hour * HISTORY_MULTIPLIER * 3600,
                                   (MIN_SAMPLES - 1) * SAMPLE_PERIOD_SECONDS)
                    if (hour in (1, 3, 6) and span >= required and
                            sample_count >= MIN_SAMPLES and
                            point.get("temp") is not None and
                            point.get("rh") is not None):
                        qualified.append(point)
                s3_prediction["points"] = qualified
                s3_prediction["status"] = "ready" if qualified else "warming_up"
        s3_crossings = []
        if isinstance(s3_prediction, dict) and s3_prediction.get("status") == "ready":
            points = s3_prediction.get("points", [])
            for channel in ("temp", "rh"):
                normalized = {"status": "ready", "forecast": [
                    {"hour": point.get("hour"),
                     "temperature_c": point.get("temp") if channel == "temp" else None,
                     "rh_pct": point.get("rh") if channel == "rh" else None}
                    for point in points if isinstance(point, dict)]}
                crossing = _forecast_crossing(normalized, channel, cfg)
                if crossing:
                    crossing.update({"channel": channel, "source": "s3"})
                    s3_crossings.append(crossing)

        weather_crossings = []
        if _weather_forecast_is_complete(weather_prediction):
            for channel in ("temp", "rh"):
                crossing = _forecast_crossing(weather_prediction, channel, cfg)
                if crossing:
                    crossing.update({"channel": channel,
                                     "source": "weather_forecast"})
                    weather_crossings.append(crossing)

        self._json(200, {
            "ok": True, "uid": uid, "addr": addr,
            "current": ({"temperature_c": current.get("temp"),
                         "rh_pct": current.get("rh"), "ts": current.get("ts")}
                        if current else None),
            "station_prediction": station_prediction,
            "s3_prediction": s3_prediction,
            "weather_prediction": weather_prediction,
            "thresholds": {"temperature_low_c": float(cfg["temp_low"]),
                           "temperature_high_c": float(cfg["temp_high"]),
                           "rh_low_pct": float(cfg["rh_low"]),
                           "rh_high_pct": float(cfg["rh_high"])},
            "risk": {"measured": measured,
                     "station_forecast": station_crossings,
                     "s3_forecast": s3_crossings,
                     "weather_forecast": weather_crossings,
                     "active": bool(measured or station_crossings or
                                    s3_crossings or weather_crossings)},
            "note": "电脑端趋势预测、天气辅助预测与 S3 预测分别展示。天气辅助模型仅在逐小时配对历史足够且时间序列留出验证优于持续性基线时显示数值；结果为探杆周围空气温湿度估计，不是粮粒含水率或整堆粮温。"
        })

    def _forecast(self, q):
        """Aggregate only qualified per-probe air trends; weather is context only."""
        args = urllib.parse.parse_qs(q, keep_blank_values=True)
        try:
            pile_id = int((args.get("pile") or [""])[0])
            if pile_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            pile_id = None
        try:
            hours = max(1, min(int((args.get("hours") or ["6"])[0]), 24))
        except (TypeError, ValueError):
            hours = 6
        def coordinate(name, lower, upper):
            try:
                value = float((args.get(name) or [""])[0])
                return value if lower <= value <= upper else None
            except (TypeError, ValueError):
                return None
        lat = coordinate("lat", -90.0, 90.0)
        lon = coordinate("lon", -180.0, 180.0)

        conn = db()
        if lat is None or lon is None:
            saved = conn.execute(
                "SELECT lat,lon FROM warehouse_weather WHERE warehouse_id=?",
                (self.warehouse_id,)).fetchone()
            if saved:
                lat, lon = float(saved[0]), float(saved[1])
        cfg = get_cfg(conn, self.warehouse_id)
        _heap, _grid, samples = _idw_grid(cfg, pile_id, self.warehouse_id)
        now = int(time.time())
        current_temp = [float(s["t"]) for s in samples
                        if s.get("t") is not None and s.get("online")]
        current_rh = [float(s["h"]) for s in samples
                      if s.get("h") is not None and s.get("online")]
        current = {
            "sample_count": len(current_temp),
            "mean_c": round(sum(current_temp) / len(current_temp), 2)
                      if current_temp else None,
            "min_c": round(min(current_temp), 2) if current_temp else None,
            "max_c": round(max(current_temp), 2) if current_temp else None,
            "rh_mean_pct": round(sum(current_rh) / len(current_rh), 2)
                           if current_rh else None,
            "rh_min_pct": round(min(current_rh), 2) if current_rh else None,
            "rh_max_pct": round(max(current_rh), 2) if current_rh else None,
            "ts": max((s.get("ts", 0) for s in samples), default=0),
        }
        weather = _cached_weather_run(conn, lat, lon, now,
                                      warehouse_id=self.warehouse_id)
        if weather is None:
            weather = _open_meteo(lat, lon, 48)
            _store_weather_run(conn, lat, lon, weather, now, self.warehouse_id)
        elif weather.get("status") == "ready":
            weather = dict(weather)
        if weather.get("status") == "ready":
            weather["series"] = (weather.get("series") or [])[:hours]
        by_horizon = {hour: [] for hour in (1, 3, 6) if hour <= hours}
        history_cache = {}
        for sample in samples:
            if not sample.get("online"):
                continue
            key = (sample["uid"], int(sample["addr"]))
            if key not in history_cache:
                history_cache[key] = predict_air_state(
                    _probe_samples(conn, key[0], key[1]), now_ts=now)
            for point in history_cache[key].get("forecast", []):
                if point["hour"] in by_horizon:
                    by_horizon[point["hour"]].append({
                        "uid": key[0], "addr": key[1],
                        "temperature_c": point["temperature_c"],
                        "rh_pct": point["rh_pct"],
                        "history_span_seconds": point["history_span_seconds"],
                    })
        conn.close()

        forecast = []
        for hour, nodes in by_horizon.items():
            if not nodes:
                continue
            temperatures = [node["temperature_c"] for node in nodes]
            humidities = [node["rh_pct"] for node in nodes]
            forecast.append({
                "hour": hour, "ts": now + hour * 3600,
                "temperature_c": round(sum(temperatures) / len(temperatures), 2),
                "temperature_min_c": round(min(temperatures), 2),
                "temperature_max_c": round(max(temperatures), 2),
                "rh_pct": round(sum(humidities) / len(humidities), 2),
                "rh_min_pct": round(min(humidities), 2),
                "rh_max_pct": round(max(humidities), 2),
                "sample_count": len(nodes), "nodes": nodes,
            })
        note = ("显示在线探杆测点周围空气温湿度的长历史趋势均值；天气只作旁证，不参与数值融合。"
                "不是粮堆内部真实温度场或粮食含水率预测；单测点不能代表整堆。"
                if forecast else
                "暂无满足历史覆盖条件的探杆预测。+1h 至少需 2.5h 历史，+3h 需 6h，+6h 需 12h。")
        self._json(200, {
            "ok": True, "pile_id": pile_id,
            "method": "probe_air_history" if forecast else "warming_up",
            "current": current, "forecast": forecast, "weather": weather,
            "coverage": {"current_probe_count": current["sample_count"],
                         "forecast_probe_count": max((len(v) for v in by_horizon.values()),
                                                     default=0)},
            "note": note,
        })

    def _static(self, p):
        if p == "/":
            p = "/index.html"
        web_root = os.path.realpath(os.path.abspath(WEB))
        relative_path = urllib.parse.unquote(p).lstrip("/\\")
        path = os.path.realpath(os.path.abspath(os.path.join(web_root, relative_path)))
        try:
            common_path = os.path.commonpath((web_root, path))
        except ValueError:
            common_path = ""
        if (os.path.normcase(common_path) != os.path.normcase(web_root) or
                not os.path.isfile(path)):
            self._json(404, {"ok": False, "err": "not found"})
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def _xml_safe(value):
    text = str(value)
    return "".join(char if char in "\t\n\r" or
                   0x20 <= ord(char) <= 0xD7FF or
                   0xE000 <= ord(char) <= 0xFFFD or
                   0x10000 <= ord(char) <= 0x10FFFF else "\uFFFD"
                   for char in text)


def _column_name(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xlsx_sheet(rows):
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("", main_ns)
    q = lambda tag: "{%s}%s" % (main_ns, tag)
    root = ET.Element(q("worksheet"))
    max_cols = max((len(row) for row in rows), default=1)
    max_rows = max(1, len(rows))
    ET.SubElement(root, q("dimension"), ref="A1:%s%d" % (_column_name(max_cols), max_rows))
    views = ET.SubElement(root, q("sheetViews"))
    view = ET.SubElement(views, q("sheetView"), workbookViewId="0")
    ET.SubElement(view, q("pane"), ySplit="1", topLeftCell="A2",
                  activePane="bottomLeft", state="frozen")
    ET.SubElement(root, q("sheetFormatPr"), defaultRowHeight="18")
    sheet_data = ET.SubElement(root, q("sheetData"))
    for row_number, values in enumerate(rows, 1):
        row_el = ET.SubElement(sheet_data, q("row"), r=str(row_number))
        for col_number, value in enumerate(values, 1):
            ref = "%s%d" % (_column_name(col_number), row_number)
            cell = ET.SubElement(row_el, q("c"), r=ref)
            if row_number == 1:
                cell.set("s", "1")
            if value is None:
                continue
            if isinstance(value, bool):
                cell.set("t", "b")
                ET.SubElement(cell, q("v")).text = "1" if value else "0"
            elif isinstance(value, (int, float)) and math.isfinite(float(value)):
                ET.SubElement(cell, q("v")).text = str(value)
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, q("is"))
                text_el = ET.SubElement(inline, q("t"))
                text = _xml_safe(value)
                if text != text.strip() or "\n" in text or "\t" in text:
                    text_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                text_el.text = text
    if len(rows) > 1 and max_cols:
        ET.SubElement(root, q("autoFilter"),
                      ref="A1:%s%d" % (_column_name(max_cols), len(rows)))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _xlsx_export(header, rows, kind, hours, query, timestamp):
    if len(rows) > 1_048_575:
        raise ValueError("导出记录超过单张 Excel 工作表上限，请缩小时间范围")
    labels = {"nodes": "探杆节点实测", "env": "仓内环境记录", "weather": "所在地天气模型"}
    sources = {
        "nodes": "来自探杆通过 AutoLink 上报并保存的节点快照。",
        "env": "来自中心站独立仓内环境传感器上报，不等同于探杆数据。",
        "weather": "来自天气服务模型快照，不是现场气象站实测。",
    }
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    filters = ["时间范围=%s小时" % hours]
    for key, label in (("pile_id", "粮堆ID"), ("uid", "探杆UID")):
        value = (params.get(key) or [""])[0]
        if value:
            filters.append("%s=%s" % (label, _xml_safe(value)))
    metadata = [
        ["字段", "说明"],
        ["数据类别", labels[kind]],
        ["数据来源边界", sources[kind]],
        ["筛选条件", "；".join(filters)],
        ["导出行数", len(rows)],
        ["导出时间（UTC）", timestamp],
        ["列定义", "、".join(_xml_safe(item) for item in header)],
    ]
    entries = {
        "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        b'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        b'</Types>',
        "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        b'</Relationships>',
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="数据" sheetId="1" r:id="rId1"/>'
            '<sheet name="导出说明" sheetId="2" r:id="rId2"/></sheets></workbook>'
        ).encode("utf-8"),
        "xl/_rels/workbook.xml.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        b'<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        b'</Relationships>',
        "xl/styles.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        b'<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        b'<fills count="2"><fill><patternFill patternType="none"/></fill>'
        b'<fill><patternFill patternType="gray125"/></fill></fills>'
        b'<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        b'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        b'<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        b'<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
        b'<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        b'</styleSheet>',
        "xl/worksheets/sheet1.xml": _xlsx_sheet([header] + rows),
        "xl/worksheets/sheet2.xml": _xlsx_sheet(metadata),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        for name, content in entries.items():
            workbook.writestr(name, content)
    return output.getvalue()


if __name__ == "__main__":
    os.makedirs(DATA_ROOT, exist_ok=True)
    if not os.path.isdir(WEB):
        raise RuntimeError(f"找不到随程序发布的网页资源目录：{WEB}")
    init_db()
    threading.Thread(target=_weather_archive_worker,
                     name="weather-archive", daemon=True).start()
    args = [arg for arg in sys.argv[1:] if arg != "--no-browser"]
    port = int(args[0]) if args else 8000
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[STATION] 中心站已启动  http://127.0.0.1:{port}")
    print(f"[STATION] 数据库 {DB}  杆子上报入口 POST /api/v1/snapshot")
    if "--no-browser" not in sys.argv[1:]:
        webbrowser.open(f"http://127.0.0.1:{port}")
    srv.serve_forever()
