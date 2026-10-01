/**
 * web.cpp - 调试模式单杆网页（V1.1）
 * 仅 MODE_DEBUG 下启用：WiFi 常开 + WebServer:80
 *   GET  /           单杆调试页（dark navy 卡片风格，3s 自动刷新）
 *   GET  /api/status 状态 JSON（uid/模式/告警/周期/节点表）
 *   POST /api/wifi   ssid/pass/host 存 NVS（重启生效）
 *   POST /api/mode   mode=debug|lowpower 切换并重启
 *   POST /api/th     th/tl/rh/poll/fast 阈值设置（℃/%RH/秒）
 *   POST /api/report 立即上报一次快照到中心站
 */
#include "web.h"
#include <Arduino.h>
#include <WebServer.h>
#include <esp_timer.h>
#include "mode.h"
#include "alarm.h"
#include "net.h"

static WebServer srv(80);

static const char *sensorName(uint8_t t)
{
    switch (t) {
    case GS_SENSOR_SHT31:    return "SHT31";
    case GS_SENSOR_GENERIC:  return "GENERIC";
    default:                 return "RESERVED";
    }
}

static const char *forecastStatusName(gs_unified_status_t status)
{
    switch (status) {
    case GS_UF_READY: return "ready";
    case GS_UF_CANDIDATE: return "candidate";
    case GS_UF_BASELINE: return "baseline";
    case GS_UF_STALE: return "stale";
    default: return "unavailable";
    }
}

static String forecastJson(const gs_unified_forecast_t &forecast)
{
    String json;
            json.reserve(760);
    json = "{\"status\":\"";
    json += forecastStatusName(forecast.status);
    json += "\",\"model_version\":";
    json += forecast.model_version;
    json += ",\"validated_mask\":";
    json += forecast.validated_mask;
    json += ",\"peer_count\":";
    json += forecast.peer_count;
    json += ",\"weather_used\":";
    json += forecast.weather_used ? "true" : "false";
    json += ",\"latest_age_ms\":";
    const uint64_t now_ms = (uint64_t)esp_timer_get_time() / 1000u;
    json += (unsigned long)(forecast.latest_timestamp_ms <= now_ms ?
        now_ms - forecast.latest_timestamp_ms : 0u);
    json += ",\"points\":[";
    if (forecast.status == GS_UF_READY || forecast.status == GS_UF_CANDIDATE ||
        forecast.status == GS_UF_BASELINE) {
        bool first = true;
        for (uint8_t i = 0u; i < GS_UF_HORIZON_COUNT; ++i) {
            const gs_unified_forecast_point_t &point = forecast.points[i];
            if (!point.valid) continue;
            if (!first) json += ",";
            json += "{\"hour\":";
            json += point.hour;
            json += ",\"temp\":";
            json += String(point.temperature_c, 2);
            json += ",\"rh\":";
            json += String(point.relative_humidity_pct, 2);
            json += ",\"candidate_available\":";
            json += point.candidate_available ? "true" : "false";
            json += ",\"candidate_temp\":";
            json += String(point.candidate_temperature_c, 2);
            json += ",\"candidate_rh\":";
            json += String(point.candidate_relative_humidity_pct, 2);
            json += ",\"temperature_validated\":";
            json += point.temperature_validated ? "true" : "false";
            json += ",\"rh_validated\":";
            json += point.rh_validated ? "true" : "false";
            json += "}";
            first = false;
        }
    }
    json += "]}";
    return json;
}

static String statusJson(void)
{
    String s = "{\"uid\":\"";
    s += poleUidStr();
    s += "\",\"mode\":\"";
    s += mode_name(mode_get());
    s += "\",\"alarm\":";
    s += alarm_is_active() ? "1" : "0";
    s += ",\"node_count\":";
    s += s_nodeCount;
    s += ",\"sample_age_ms\":";
    s += s_last_sample_ms ? String((unsigned long)(millis() - s_last_sample_ms)) : String("null");
    s += ",\"interval_ms\":";
    s += (unsigned long)g_sample_interval_ms;
    s += ",\"sample_interval_ms\":";
    s += (unsigned long)g_sample_interval_ms;
    s += ",\"report_interval_ms\":";
    s += (unsigned long)g_report_interval_ms;
    s += ",\"adaptive_fast\":";
    s += g_adaptive_fast ? "true" : "false";
    s += ",\"measurement_risk\":";
    s += g_measurement_risk ? "true" : "false";
    s += ",\"prediction_risk\":";
    s += g_prediction_risk ? "true" : "false";
    const alarm_cfg_t *remote_cfg = alarm_get_cfg();
    s += ",\"normal_interval_ms\":";
    s += remote_cfg ? (unsigned long)remote_cfg->normalIntervalMs : 0ul;
    s += ",\"fast_interval_ms\":";
    s += remote_cfg ? (unsigned long)remote_cfg->fastIntervalMs : 0ul;
    s += ",\"remote_config_revision\":";
    s += (unsigned long)alarm_remote_config_revision();
    s += ",\"forecast_model_version\":";
    s += (unsigned long)g_unified_model.version;
    s += ",\"forecast_validated_mask\":";
    s += (unsigned)g_unified_model.validated_mask;
    s += ",\"report_queue_replacements\":";
    s += (unsigned long)g_report_queue_replacements;
    s += ",\"rssi\":";
    s += net_rssi();
    s += ",\"wifi_connected\":";
    s += net_wifi_connected() ? "true" : "false";
    s += ",\"station_configured\":";
    s += net_station_host_configured() ? "true" : "false";
    s += ",\"nodes\":[";

    bool first = true;
    for (uint8_t i = 0; i < s_nodeCount; i++) {
        const SnapNode *sn = &g_snap[i];
        if (!sn->valid) {
            continue;
        }
        if (!first) s += ",";
        first = false;

        float temp = NAN, rh = NAN;
        for (uint8_t k = 0; k < sn->nch; k++) {
            if (sn->ch[k].id == GS_CH_TEMP) temp = sn->ch[k].raw / 100.0f;
            if (sn->ch[k].id == GS_CH_RH)   rh   = sn->ch[k].raw / 100.0f;
        }
        s += "{\"addr\":";
        s += sn->addr;
        s += ",\"stype\":\"";
        s += sensorName(sn->stype);
        s += "\",\"temp\":";
        s += isnan(temp) ? String("null") : String(temp, 2);
        s += ",\"rh\":";
        s += isnan(rh) ? String("null") : String(rh, 2);
        s += ",\"status\":";
        s += sn->status;
        s += ",\"sample_boot_id\":";
        s += (unsigned long)sn->sample_boot_id;
        s += ",\"sample_seq\":";
        s += (unsigned long)sn->sample_seq;
        s += ",\"sample_age_ms\":";
        s += (unsigned long)(millis() - sn->sampled_at_ms);
        s += ",\"last_wire_error\":";
        s += sn->last_wire_error;
        s += ",\"wire_error_count\":";
        s += (unsigned long)sn->wire_error_count;
        s += ",\"wire_error_age_ms\":";
        s += sn->wire_error_count ?
             String((unsigned long)(millis() - sn->last_wire_error_at_ms)) :
             String("null");
        s += ",\"forecast\":";
        s += forecastJson(g_unified_forecasts[i]);
        s += "}";
    }
    s += "]}";
    return s;
}

static const char PAGE_HTML[] = R"html(<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#f6f9f7">
<title>粮仓探杆 · S3 设备</title>
<style>
:root{color-scheme:light;--bg:#f5f8f6;--surface:#fff;--line:#dbe6e1;--ink:#233a36;--muted:#73847f;--teal:#128b7d;--deep:#0c685f;--mint:#e6f3ed;--gold:#c99a4a;--warn:#cb8514;--red:#c94d47;--shadow:0 6px 20px rgba(39,73,63,.055)}
*{box-sizing:border-box}html{min-height:100%;scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px}button,input,select{font:inherit}button{cursor:pointer}.device-shell{min-height:100vh}.side-nav{position:fixed;inset:0 auto 0 0;z-index:4;width:276px;padding:27px 20px;background:#fff;border-right:1px solid var(--line);display:flex;flex-direction:column}.side-brand{display:flex;align-items:center;gap:12px;padding:0 12px 31px}.side-brand b{display:block;color:#14584f;font-size:21px;letter-spacing:.05em}.side-brand small{display:block;margin-top:4px;color:var(--muted);font-size:12px}.grain-mark{width:40px;height:42px;color:#b7893f;flex:none}.side-nav nav{display:grid;gap:7px}.side-nav a{display:flex;align-items:center;gap:15px;min-height:57px;padding:0 17px;border-radius:10px;color:#506660;text-decoration:none;font-size:15px;font-weight:600}.side-nav a:hover,.side-nav a.active{background:var(--mint);color:var(--deep)}.side-nav a.active{box-shadow:inset 4px 0 var(--teal)}.nav-icon{width:22px;height:22px;color:currentColor;flex:none}.nav-foot{margin-top:auto;padding:13px 12px;color:var(--muted);font-size:11px;border-top:1px solid #edf1ee}.nav-foot i{display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:#45a87c}
.device-main{width:min(1440px,calc(100% - 276px));margin-left:276px;padding:34px 32px 42px}.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:8px}.title-row{display:flex;align-items:center;gap:17px}.page-head h1{margin:0;color:#173f39;font-size:30px;letter-spacing:.01em}.page-time{padding-top:10px;color:#657872;font-size:14px;white-space:nowrap}.sub{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:12px 0 20px;color:#73847f;font-size:16px}.uid-chip{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;border:1px solid #dce9e2;border-radius:999px;background:#fff;color:#71817b;font-size:11px}.uid-chip b{color:#3b5c52;font-weight:600}.status-pill{display:inline-flex;align-items:center;gap:9px;padding:10px 18px;border:1px solid #bfe3d2;border-radius:999px;background:#f0f8f3;color:#227658;font-size:14px;font-weight:600}.status-pill i,.metric-dot{width:10px;height:10px;border-radius:50%;background:#32a66e;box-shadow:0 0 0 4px rgba(50,166,110,.11)}.status-pill.offline{border-color:#f0c7c2;background:#fff6f5;color:var(--red)}.status-pill.offline i{background:var(--red);box-shadow:0 0 0 4px rgba(201,77,71,.1)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:0 0 16px;padding:14px 7px}.metric{display:flex;align-items:center;gap:16px;min-height:92px;padding:12px 22px;border-right:1px solid #dce5e1}.metric:last-child{border:0}.metric-icon{display:grid;place-items:center;width:51px;height:51px;border-radius:14px;background:#eff8f4;color:var(--teal);flex:none}.metric-icon.warn{background:#fff6e9;color:var(--warn)}.metric-icon svg{width:30px;height:30px}.metric-label{display:block;margin-bottom:7px;color:#667872;font-size:14px}.metric strong{display:block;color:#17685d;font-size:20px;font-weight:700;line-height:1.2}.metric small{display:block;margin-top:5px;color:#84918d;font-size:12px}.alarm-value.active{color:var(--red)}
.node-card{overflow:hidden}.node-head{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:18px 22px;border-bottom:1px solid #e7eeea}.node-heading{display:flex;align-items:center;gap:11px}.node-heading svg{width:26px;height:26px;color:var(--teal)}.node-heading h2{margin:0;color:#29463f;font-size:19px}.pager{display:flex;align-items:center;gap:13px;color:#34534b;font-weight:600}.icon-button{display:grid;place-items:center;width:43px;height:43px;border:1px solid #dfe8e3;border-radius:9px;background:#f8faf9;color:#36544c;font-size:23px}.icon-button:hover{background:var(--mint);color:var(--deep)}.icon-button:disabled{opacity:.42;cursor:not-allowed}.node-layout{display:grid;grid-template-columns:minmax(0,1fr);align-items:center;gap:18px;padding:20px 24px 22px}.node-data{min-width:0}.identity-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px;padding-bottom:18px;border-bottom:1px solid #e7eeea}.identity-item span,.reading-label{display:block;color:#73847f;font-size:13px}.identity-item b{display:block;margin-top:7px;color:#344c46;font-size:16px;font-weight:600;overflow-wrap:anywhere}.readings{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:23px 0 21px}.reading{display:flex;align-items:center;gap:13px;min-width:0}.reading svg{width:34px;height:34px;color:var(--teal);flex:none}.reading strong{display:block;margin-top:5px;color:#17685d;font-size:34px;line-height:1.1;font-variant-numeric:tabular-nums}.reading strong small{font-size:17px}.reading.empty strong{color:#a0aaa6;font-size:25px}.actions{display:flex;flex-wrap:wrap;gap:11px}.actions form{flex:1}.actions form button{width:100%}.button{min-height:46px;padding:0 21px;border:1px solid var(--teal);border-radius:8px;background:var(--teal);color:#fff;font-weight:600}.button:hover{background:var(--deep)}.button.secondary{background:#fff;color:var(--deep)}.button.secondary:hover{background:#f0f8f4}.button svg{width:18px;height:18px;margin-right:8px;vertical-align:middle}
.all-nodes{margin-top:0;border-top:1px solid #e7eeea}.all-nodes summary{padding:16px 22px;color:#3d5a52;font-weight:600;cursor:pointer;list-style:none}.all-nodes summary::-webkit-details-marker{display:none}.all-nodes summary:after{content:"＋";float:right;color:var(--teal)}.all-nodes[open] summary:after{content:"－"}.table-wrap{overflow-x:auto;padding:0 17px 16px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:11px 12px;text-align:left;white-space:nowrap;border-bottom:1px solid #e7eeea}th{color:#768680;background:#f7faf8;font-weight:500}td{color:#3d514b}.badge{display:inline-flex;align-items:center;gap:7px;padding:4px 9px;border-radius:999px;background:#edf7f1;color:#287b58;font-size:12px}.badge:before{content:"";width:7px;height:7px;border-radius:50%;background:#39a66f}.badge.off{background:#fff0ee;color:#b8413b}.badge.off:before{background:var(--red)}
.settings-stack{display:grid;gap:9px;margin-top:14px}.settings{overflow:hidden}.settings summary{display:flex;align-items:center;gap:14px;min-height:59px;padding:0 19px;color:#35564d;font-size:16px;font-weight:600;cursor:pointer;list-style:none}.settings summary::-webkit-details-marker{display:none}.settings summary:after{content:"⌄";margin-left:auto;color:#668078;font-size:22px;transition:transform .18s}.settings[open] summary:after{transform:rotate(180deg)}.settings-icon{width:23px;height:23px;color:var(--teal)}.settings-body{padding:16px 20px 19px;border-top:1px solid #e8efeb}.settings-body form{display:flex;align-items:end;flex-wrap:wrap;gap:11px 13px}.field{display:grid;gap:6px;min-width:145px;flex:1}.field label{color:#71817b;font-size:12px}.field input,.field select{width:100%;min-height:41px;padding:8px 10px;border:1px solid #dce7e1;border-radius:7px;background:#fbfdfb;color:#2f4941}.field input:focus,.field select:focus{outline:2px solid #9ed3bd;outline-offset:1px}.settings-note{margin:0 0 12px;color:#73847f;font-size:12px;line-height:1.6}.inline-actions{display:flex;gap:10px;align-items:center}.hint{color:#72817b;font-size:12px;line-height:1.7}.hint p{margin:8px 0}.hint code{padding:2px 5px;border-radius:4px;background:#eff5f1;color:#395a4f}
.restart-card{display:flex;align-items:center;gap:17px;margin-top:14px;padding:17px 20px;border-color:#f0dfdc;background:#fffdfd}.restart-mark{display:grid;place-items:center;width:42px;height:42px;flex:none;color:var(--red)}.restart-mark svg{width:31px;height:31px}.restart-copy{min-width:0;flex:1}.restart-copy b{display:block;color:#bd443f;font-size:16px}.restart-copy span{display:block;margin-top:5px;color:#77837f;font-size:13px;line-height:1.5}.button.danger{border-color:#db6b64;background:#fff;color:#bf4943}.button.danger:hover{background:#fff3f2}.toast{position:fixed;right:20px;bottom:20px;z-index:9;max-width:min(420px,calc(100vw - 40px));padding:13px 16px;border:1px solid #cce4d7;border-radius:9px;background:#f5fcf7;color:#24694d;box-shadow:0 10px 30px #203b3620;font-size:13px}.toast.error{border-color:#f0c8c3;background:#fff7f6;color:#ad3933}[hidden]{display:none!important}
@media(max-width:1100px){.side-nav{width:218px;padding:23px 14px}.device-main{width:calc(100% - 218px);margin-left:218px;padding:25px 22px 32px}.metric{padding:10px 13px;gap:11px}.node-layout{grid-template-columns:1fr .85fr;gap:8px;padding:17px}.reading strong{font-size:29px}}
@media(max-width:760px){.side-nav{position:sticky;inset:0 0 auto;width:100%;height:auto;min-height:58px;padding:7px 10px;border-right:0;border-bottom:1px solid var(--line);flex-direction:row;align-items:center;overflow-x:auto}.side-brand{padding:0 5px 0 2px;margin-right:3px}.side-brand b{font-size:14px;white-space:nowrap}.side-brand small,.nav-foot{display:none}.grain-mark{width:29px;height:32px}.side-nav nav{display:flex;gap:2px}.side-nav a{min-height:42px;padding:0 10px;gap:6px;font-size:11px;white-space:nowrap}.nav-icon{width:17px;height:17px}.device-main{width:100%;margin:0;padding:20px 14px 28px}.page-head{align-items:flex-start}.page-head h1{font-size:23px}.title-row{gap:10px;flex-wrap:wrap}.status-pill{padding:7px 11px;font-size:12px}.page-time{padding-top:5px;font-size:11px}.sub{margin:9px 0 15px;font-size:13px}.uid-chip{padding:4px 8px}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr));padding:7px;margin-bottom:12px}.metric{min-height:76px;padding:9px 10px;border-right:1px solid #e6eeea}.metric:nth-child(2){border-right:0}.metric:nth-child(-n+2){border-bottom:1px solid #e6eeea}.metric-icon{width:39px;height:39px;border-radius:11px}.metric-icon svg{width:23px;height:23px}.metric-label{font-size:11px;margin-bottom:4px}.metric strong{font-size:15px}.metric small{font-size:10px}.node-head{padding:13px 14px}.node-heading h2{font-size:16px}.node-heading svg{width:22px;height:22px}.pager{gap:8px;font-size:13px}.icon-button{width:35px;height:35px}.node-layout{grid-template-columns:1fr;padding:15px;gap:10px}.node-data{order:1}.silo-figure{order:2;max-width:430px;width:100%;margin:0 auto}.silo-figure svg{max-height:250px}.identity-grid{gap:8px}.identity-item span,.reading-label{font-size:11px}.identity-item b{font-size:13px}.readings{padding:17px 0;gap:8px}.reading{gap:8px}.reading svg{width:27px;height:27px}.reading strong{font-size:27px}.reading strong small{font-size:13px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.button{min-height:43px;padding:0 10px;font-size:12px}.button svg{margin-right:4px;width:16px}.all-nodes summary{padding:14px 15px;font-size:13px}.table-wrap{padding:0 9px 12px}th,td{padding:9px 8px;font-size:11px}.settings summary{min-height:52px;padding:0 14px;font-size:14px}.settings-body{padding:13px}.settings-body form{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{min-width:0}.settings-body form .button{grid-column:1/-1}.restart-card{gap:9px;padding:13px}.restart-mark{width:30px}.restart-copy b{font-size:14px}.restart-copy span{font-size:11px}.restart-card .button{padding:0 12px}}
@media(max-width:390px){.side-brand{display:none}.page-head{display:block}.page-time{margin-top:8px}.readings{grid-template-columns:1fr}.reading{min-height:54px}.identity-grid{grid-template-columns:1fr 1fr}.actions{grid-template-columns:1fr}.restart-card{flex-wrap:wrap}.restart-card .button{margin-left:39px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*:before,*:after{transition:none!important;animation:none!important}}
</style>
<style>
.silo-figure{min-width:0;margin:0;padding:0}.scene-canvas{position:relative;overflow:hidden;border:1px solid #d5e4dc;border-radius:14px;background:#eef5f0;box-shadow:0 12px 28px rgba(40,72,62,.11)}.scene-canvas>img{display:block;width:100%;height:auto;aspect-ratio:720/552;object-fit:cover}.scene-canvas #probeOverlay{position:absolute;inset:0;width:100%;height:100%;max-height:none;pointer-events:none}.scene-empty{fill:#173f39;stroke:#f6fbf8;stroke-width:3px;paint-order:stroke; font:600 15px "Segoe UI","Microsoft YaHei",sans-serif}.scene-badge{position:absolute;top:10px;right:10px;display:flex;align-items:center;gap:7px;padding:7px 10px;border:1px solid #d7e8df;border-radius:999px;background:rgba(255,255,255,.92);color:#355d52;font-size:11px;box-shadow:0 3px 12px #183a3020}.scene-badge i{width:8px;height:8px;border-radius:50%;background:#218b79}.scene-note{margin:9px 2px 0;color:#6d8179;font-size:11px;line-height:1.55}.silo-figure figcaption{margin-top:5px;color:#71847c;text-align:center;font-size:11px;line-height:1.5}.settings-network-state{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:4px 10px;margin:0 0 14px;padding:12px 14px;border:1px solid #dcebe3;border-radius:9px;background:#f5faf7}.settings-network-state span{color:#6d8179;font-size:12px}.settings-network-state b{color:#17685d;font-size:14px}.settings-network-state small{grid-column:1/-1;color:#71847c;font-size:11px;line-height:1.5}.inventory-card{padding:0 18px 15px}.inventory-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:17px 2px 12px;border-bottom:1px solid #e8efeb}.inventory-head h2{margin:0;color:#29463f;font-size:19px}.inventory-head p{margin:5px 0 0;color:#73847f;font-size:12px;line-height:1.5}.inventory-head>b{flex:none;padding:7px 11px;border:1px solid #cfe5d9;border-radius:999px;background:#f1f8f4;color:#24775a;font-size:12px}.node-inventory{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding-top:13px}.inventory-node{min-width:0;padding:14px;border:1px solid #e0eae5;border-radius:10px;background:linear-gradient(140deg,#fff,#f7fbf8)}.inventory-node-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.inventory-node-head b{color:#22564d;font-size:16px}.inventory-role{margin:9px 0;color:#62766e;font-size:12px;line-height:1.5}.inventory-values{display:grid;grid-template-columns:1fr 1fr;gap:8px}.inventory-value{padding:9px;border-radius:8px;background:#eef6f1}.inventory-value span{display:block;color:#71847b;font-size:10px}.inventory-value strong{display:block;margin-top:4px;color:#17685d;font-size:18px;font-variant-numeric:tabular-nums}.inventory-empty{grid-column:1/-1;padding:18px;border:1px dashed #cfded6;border-radius:9px;color:#788981;text-align:center;font-size:13px}.inventory-footnote{margin:12px 2px 0;color:#73847f;font-size:11px;line-height:1.55}
@media(max-width:760px){.scene-badge{top:7px;right:7px;padding:5px 8px;font-size:10px}.node-inventory{grid-template-columns:1fr}.inventory-card{padding:0 12px 13px}.inventory-head h2{font-size:16px}.inventory-node{padding:12px}}
</style>
<style>
.node-forecast{margin:0 0 18px;padding:14px;border:1px solid #dcebe3;border-radius:10px;background:linear-gradient(135deg,#f5faf7,#fff)}.node-forecast-head{display:flex;align-items:center;justify-content:space-between;gap:10px;color:#28584e}.node-forecast-head span{padding:4px 8px;border-radius:999px;background:#e5f3eb;color:#37745d;font-size:11px}.node-forecast p{margin:7px 0 10px;color:#71847c;font-size:11px;line-height:1.5}.node-forecast-points{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.node-forecast-points>div{display:grid;gap:3px;padding:9px;border-radius:8px;background:#eef6f1}.node-forecast-points small,.node-forecast-points span{color:#70827b;font-size:10px}.node-forecast-points b{color:#17685d;font-size:15px}.node-forecast-points .candidate-preview{padding:5px 0 0;color:#94651a;font-size:9px;line-height:1.45}.node-forecast-points>span{grid-column:1/-1;padding:4px 0;line-height:1.5}
@media(max-width:760px){.node-forecast{margin-bottom:14px;padding:11px}.node-forecast-points{gap:5px}.node-forecast-points>div{padding:7px}.node-forecast-points b{font-size:13px}}
.home-readings{margin:0 0 16px;padding:17px 20px}.home-readings-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:13px}.home-readings-head h2{margin:0;color:#29463f;font-size:17px}.home-readings-head p{margin:4px 0 0;color:#73847f;font-size:11px}.home-readings-head a{color:#0c685f;font-size:12px;font-weight:600;text-decoration:none}.home-readings-head a:hover{text-decoration:underline}.home-node-list{display:grid;gap:8px}.home-node-row{display:grid;grid-template-columns:minmax(130px,1.2fr) minmax(95px,.8fr) minmax(105px,.9fr) auto;align-items:center;gap:14px;padding:10px 13px;border:1px solid #e5eee9;border-radius:9px;background:#f8fbf9;color:#344c46;text-decoration:none;transition:background .15s,border-color .15s}.home-node-row:hover{border-color:#b8d9ca;background:#f0f8f3}.home-node-identity b,.home-node-value b{display:block;color:#17685d;font-size:15px;font-variant-numeric:tabular-nums}.home-node-identity small,.home-node-value small{display:block;margin-top:3px;color:#73847f;font-size:11px}.home-node-value b{color:#29463f}.home-node-row .badge{justify-self:end}.home-node-empty{padding:16px;border-radius:9px;background:#f7faf8;color:#73847f;line-height:1.6}
@media(max-width:760px){.home-readings{padding:13px}.home-readings-head h2{font-size:15px}.home-node-row{grid-template-columns:minmax(95px,1fr) minmax(78px,auto) minmax(86px,auto);gap:8px;padding:9px}.home-node-row .badge{grid-column:1/-1;justify-self:start}.home-node-identity b,.home-node-value b{font-size:13px}}
@media(max-width:390px){.home-readings-head{align-items:flex-start}.home-node-row{grid-template-columns:1fr 1fr;gap:8px}.home-node-row .badge{grid-column:auto;justify-self:start}}
</style>
</head>
<body>
<div class="device-shell">
<aside class="side-nav" aria-label="设备导航">
  <div class="side-brand"><svg class="grain-mark" viewBox="0 0 40 42" role="img" aria-label="谷穗"><path d="M20 39V10M20 21C10 21 6 15 7 9c8 0 13 4 13 12ZM20 30C10 30 5 25 5 19c9 0 15 4 15 11ZM20 17c10 0 14-6 13-12-8 0-13 4-13 12ZM20 28c10 0 15-5 15-11-9 0-15 4-15 11Z" fill="currentColor"/><path d="M14 39h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><div><b>粮仓探杆</b><small>设备现场管理</small></div></div>
  <nav>
    <a class="active" href="#device" data-route="device" aria-current="page"><svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 7h6M8 12h8M8 16h5"/></svg><span>设备总览</span></a>
    <a href="#data" data-route="data"><svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 19V9m5 10V5m5 14v-7m5 7V3"/><path d="M2 21h20"/></svg><span>节点数据</span></a>
    <a href="#settings" data-route="settings"><svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="m19.4 15 .1.1 1.2 1-.1 1.4-1.4 2.4-1.3.4-1.4-.6a7.7 7.7 0 0 1-1.6.9l-.3 1.5-1.1.8h-2.8l-1.1-.8-.3-1.5a7.7 7.7 0 0 1-1.6-.9l-1.4.6-1.3-.4-1.4-2.4-.1-1.4 1.2-1a7.4 7.4 0 0 1 0-1.9l-1.2-1 .1-1.4 1.4-2.4 1.3-.4 1.4.6a7.7 7.7 0 0 1 1.6-.9l.3-1.5 1.1-.8h2.8l1.1.8.3 1.5a7.7 7.7 0 0 1 1.6.9l1.4-.6 1.3.4 1.4 2.4.1 1.4-1.2 1a7.4 7.4 0 0 1 0 1.8Z" transform="translate(-.8 -1) scale(1.07)"/></svg><span>联网与告警</span></a>
    <a href="#about" data-route="about"><svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg><span>节点清单</span></a>
  </nav>
  <div class="nav-foot"><i></i>本地设备配置页面</div>
</aside>
<main class="device-main">
<header class="page-head"><div><div class="title-row"><h1 id="pageTitle">设备总览</h1><span class="status-pill" id="status"><i></i><span id="connText">正在连接</span></span></div><p class="sub" id="deviceSummary"><span id="summaryText">正在读取设备状态…</span><span class="uid-chip">设备编号 <b id="deviceUid">—</b></span></p></div><div class="page-time" id="lastOk">等待连接</div></header>
<div class="page-view" id="device" data-page="device">
<section class="card metric-grid" aria-label="设备运行摘要">
  <div class="metric"><span class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></svg></span><div><span class="metric-label">最新采样</span><strong id="latestAge">等待数据</strong><small id="latestTime">暂无有效样本</small></div></div>
  <div class="metric"><span class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 19v-4m5 4V9m5 10V5m5 14V2" stroke-linecap="round"/></svg></span><div><span class="metric-label">Wi-Fi 信号</span><strong id="signalValue">读取中</strong><small id="signalHint">来自设备 Wi-Fi 状态</small></div></div>
  <div class="metric"><span class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3.4"/><path d="M12 2v3m0 14v3M4.9 4.9l2.1 2.1m10 10 2.1 2.1M2 12h3m14 0h3M4.9 19.1 7 17m10-10 2.1-2.1" stroke-linecap="round"/></svg></span><div><span class="metric-label">运行模式</span><strong id="modeValue">—</strong><small id="modeHint">采样周期待读取</small></div></div>
  <div class="metric"><span class="metric-icon warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6Zm-2 16a2 2 0 0 0 4 0" stroke-linecap="round" stroke-linejoin="round"/></svg></span><div><span class="metric-label">阈值提醒</span><strong class="alarm-value" id="alarmValue">读取中</strong><small id="alarmHint">以设备实时状态为准</small></div></div>
</section>
<section class="card home-readings" aria-label="当前节点实时温湿度" aria-live="polite"><header class="home-readings-head"><div><h2>节点实时状态</h2><p>列出 S3 最近收到的节点读数；地址与探杆上下位置的对应关系以现场对照为准。</p></div><a href="#data" data-route="data">查看曲线与详情 →</a></header><div class="home-node-list" id="homeNodes"><div class="home-node-empty">正在读取节点数据…</div></div></section>
 </div>
 <div class="page-view" id="data" data-page="data" hidden>
<section class="card node-card" id="nodes-card">
<header class="node-head"><div class="node-heading"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 15h3m2 0h3"/></svg><h2>节点测量数据</h2></div><div class="pager"><button class="icon-button" id="prevNode" type="button" aria-label="上一个节点">‹</button><span id="nodePage">节点 0 / 0</span><button class="icon-button" id="nextNode" type="button" aria-label="下一个节点">›</button></div></header>
<div class="node-layout"><div class="node-data"><div class="identity-grid"><div class="identity-item"><span>节点地址</span><b id="nodeAddr">等待节点</b></div><div class="identity-item"><span>传感器型号</span><b id="sensorType">—</b></div><div class="identity-item"><span>采样时间</span><b id="nodeSampleTime">等待数据</b></div></div>
<div class="readings"><div class="reading" id="tempReading"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 14.8V5a3 3 0 0 0-6 0v9.8a5 5 0 1 0 6 0Z"/><path d="M11 8v9"/></svg><div><span class="reading-label">温度</span><strong id="nodeTemp">— <small>°C</small></strong></div></div><div class="reading" id="rhReading"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3C9 7.4 5.5 11 5.5 15a6.5 6.5 0 0 0 13 0c0-4-3.5-7.6-6.5-12Z"/><path d="M9 16a3 3 0 0 0 3 3"/></svg><div><span class="reading-label">相对湿度</span><strong id="nodeRh">— <small>%RH</small></strong></div></div></div>
<section class="node-forecast" aria-live="polite"><div class="node-forecast-head"><b>统一温湿度预测</b><span id="nodeForecastStatus">等待数据</span></div><p id="nodeForecastMeta">电脑端训练统一模型；S3 与电脑端使用同一版本、参数和推理公式。未通过留出验证的候选值仅供观察，不参与告警或采样控制。预测对象是探杆周围空气，不是粮食含水率或整仓实测温度场。</p><div id="nodeForecastPoints" class="node-forecast-points"><span>收到有效节点实测后立即显示持续性基线。</span></div></section>
<div class="actions"><form method="post" action="/api/report"><button class="button" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 16V4m0 0L7 9m5-5 5 5M5 15v4h14v-4" stroke-linecap="round" stroke-linejoin="round"/></svg>立即上报</button></form><button class="button secondary" id="refreshNow" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 7v5h-5M4 17v-5h5" stroke-linecap="round" stroke-linejoin="round"/><path d="M6.2 9A7 7 0 0 1 18 6l2 2M4 16l2 2a7 7 0 0 0 11.8-3" stroke-linecap="round"/></svg>刷新数据</button></div>
</div></div>
<details class="all-nodes" id="allNodes"><summary>全部节点数据（<span id="allNodeCount">0</span> 个节点）</summary><div class="table-wrap"><table><thead><tr><th>地址</th><th>传感器</th><th>温度</th><th>湿度</th><th>状态</th></tr></thead><tbody id="nodes"><tr><td colspan="5">正在读取节点…</td></tr></tbody></table></div></details>
</section>
 </div>
<div class="page-view" id="settings" data-page="settings" hidden>
<section class="settings-stack" aria-label="设备配置">
<details class="card settings" id="wifiSettings" open><summary><svg class="settings-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 9a14 14 0 0 1 18 0M6 12a9 9 0 0 1 12 0m-9 3a4 4 0 0 1 6 0m-3 4h.01" stroke-linecap="round"/></svg>Wi-Fi 网络与中心站</summary><div class="settings-body"><div class="settings-network-state"><span>当前 Wi-Fi 与中心站状态</span><b id="settingsWifiState">读取中</b><small>已保存的密码不会回显；SSID 和密码都留空即可只更新电脑中心站地址。</small></div><p class="settings-note">中心站地址填写电脑局域网 IPv4（请在电脑网络设置中查看）。更改 Wi-Fi 名称/密码后需重启；仅更新地址也请重启以重新载入。</p>
<form method="post" action="/api/wifi"><div class="field"><label for="wifiSsid">Wi-Fi 名称（SSID，可留空保留现有）</label><input id="wifiSsid" name="ssid" autocomplete="off"></div><div class="field"><label for="wifiPass">Wi-Fi 密码（留空保留现有）</label><input id="wifiPass" name="pass" type="password" autocomplete="new-password"></div><div class="field"><label for="stationHost">电脑中心站 IPv4 地址</label><input id="stationHost" name="host" placeholder="电脑中心站局域网 IPv4 地址" inputmode="decimal"></div><button class="button" type="submit">保存网络配置</button></form></div></details>
<details class="card settings" id="thresholds"><summary><svg class="settings-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Zm-8 12a2 2 0 0 0 4 0" stroke-linecap="round" stroke-linejoin="round"/></svg>告警阈值与采样策略</summary><div class="settings-body"><p class="settings-note">留空的项目不修改；温度单位 ℃，相对湿度单位 %RH，周期单位秒。</p><form method="post" action="/api/th"><div class="field"><label for="thHigh">高温阈值 TH（℃）</label><input id="thHigh" name="th" inputmode="decimal" placeholder="30.0"></div><div class="field"><label for="thLow">低温阈值 TL（℃）</label><input id="thLow" name="tl" inputmode="decimal" placeholder="-5.0"></div><div class="field"><label for="rhHigh">高湿阈值 RH（%）</label><input id="rhHigh" name="rh" inputmode="decimal" placeholder="70.0"></div><div class="field"><label for="pollSec">常规采样周期（秒）</label><input id="pollSec" name="poll" inputmode="numeric" placeholder="900"></div><div class="field"><label for="fastSec">告警快速采样周期（秒）</label><input id="fastSec" name="fast" inputmode="numeric" placeholder="60"></div><button class="button" type="submit">保存阈值设置</button></form></div></details>
<details class="card settings" id="operationMode"><summary><svg class="settings-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="white"/><circle cx="15" cy="12" r="2" fill="white"/><circle cx="11" cy="18" r="2" fill="white"/></svg>设备工作模式</summary><div class="settings-body"><p class="settings-note">当前模式：<b id="settingsModeValue">读取中</b>。调试模式保持 Wi-Fi 常开；低功耗模式使用 Deep Sleep。切换后设备会重启。</p><form method="post" action="/api/mode"><div class="field"><label for="runMode">设备工作模式</label><select id="runMode" name="mode"><option value="debug">调试模式（Wi-Fi 常开）</option><option value="lowpower">低功耗模式（Deep Sleep）</option></select></div><button class="button" type="submit">切换并重启</button></form></div></details>
</section></div>
<div class="page-view" id="about" data-page="about" hidden>
<section class="settings-stack" aria-label="节点清单与设备说明">
<section class="card inventory-card" aria-labelledby="aboutTitle"><header class="inventory-head"><div><h2 id="aboutTitle">当前有效节点</h2><p>节点数量与职责来自 S3 实时状态，不预填演示节点。</p></div><b id="aboutNodeCount">等待数据</b></header><div class="node-inventory" id="aboutNodes" aria-live="polite"><div class="inventory-empty">正在读取节点清单…</div></div><p class="inventory-footnote">节点职责以实际传感器类型为准；当前页面未配置探杆安装坐标，因此图中位置仅为示意。</p></section>
<details class="card settings" id="aboutInfo"><summary><svg class="settings-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg>访问帮助</summary><div class="settings-body hint"><p>此页面用于 S3 探杆的现场快速查看与配置；完整历史、趋势和多粮仓管理由电脑端 Station 提供。</p><p>若连接设备配网热点 <b>GrainSilo-XXXX</b>，可访问 <code>http://192.168.4.1/</code>；若设备已连接路由器，请在同一局域网使用它当前的局域网地址（不是 HTTPS）。</p><p>页面每 3 秒读取一次状态；密码不从设备回显。没有有效节点时清单和场景会显示空态，不使用演示读数。</p></div></details>
</section></div>
<div class="page-view" data-page="settings" hidden>
<section class="card restart-card"><span class="restart-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v9m-6.4-6A9 9 0 1 0 20 9" stroke-linecap="round"/></svg></span><div class="restart-copy"><b>重启设备</b><span>重启后网页会短暂断开，待设备重新启动并连接后即可再次访问。</span></div><form method="post" action="/api/mode" id="restartForm"><input type="hidden" name="mode" id="restartMode" value="debug"><button class="button danger" type="submit">立即重启</button></form></section>
</div>
</main></div>
<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
<script>
/* NAV_ROUTER_BEGIN */
var routeTitles={device:'设备总览',data:'节点数据',settings:'联网与告警',about:'节点清单'};
function activatePage(page){
 if(!Object.prototype.hasOwnProperty.call(routeTitles,page))page='device';
 document.querySelectorAll('.page-view').forEach(function(view){view.hidden=view.getAttribute('data-page')!==page});
 document.querySelectorAll('.side-nav nav a[data-route]').forEach(function(link){var active=link.getAttribute('data-route')===page;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')});
 document.getElementById('pageTitle').textContent=routeTitles[page];
 window.scrollTo(0,0);
 return page;
}
function routeFromHash(){var page=String(location.hash||'').replace(/^#\/?/,'').split(/[?#]/)[0];activatePage(page)}
window.addEventListener('hashchange',routeFromHash);
routeFromHash();
/* NAV_ROUTER_END */
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
var nodeRows=[],selectedNode=0,currentMode='debug',toastTimer=0;
function setConn(ok,text){var el=document.getElementById('status');el.className='status-pill'+(ok?'':' offline');document.getElementById('connText').textContent=text;if(ok)document.getElementById('lastOk').textContent='更新于：'+new Date().toLocaleString()}
function sampleAge(ms){if(ms===null||ms===undefined||!isFinite(Number(ms)))return '等待数据';var sec=Math.max(0,Math.floor(Number(ms)/1000));if(sec<60)return sec+' 秒前';if(sec<3600)return Math.floor(sec/60)+' 分钟前';if(sec<86400)return Math.floor(sec/3600)+' 小时前';return Math.floor(sec/86400)+' 天前'}
function nodeIsStale(row){var age=Number(row&&row.sample_age_ms),interval=Number(window.sampleIntervalMs||10000);return !isFinite(age)||age>Math.max(60000,interval*3)}
function fmt(value,unit,digits){if(value===null||value===undefined||!isFinite(Number(value)))return '— <small>'+unit+'</small>';return Number(value).toFixed(digits)+' <small>'+unit+'</small>'}
function renderNodeForecast(node){
 var status=document.getElementById('nodeForecastStatus'),meta=document.getElementById('nodeForecastMeta'),host=document.getElementById('nodeForecastPoints');
 if(nodeIsStale(node)){status.textContent='节点实测数据陈旧';meta.textContent='该节点超过采样周期阈值未收到新样本；旧读数仅供追溯，不作为当前预测依据。';host.innerHTML='<span>等待节点恢复采样后再显示预测。</span>';return}
 var forecast=node&&node.forecast?node.forecast:null;
 if(!forecast){status.textContent='等待数据';meta.textContent='尚未收到有效节点实测。';host.innerHTML='<span>收到有效节点样本后立即生成基线。</span>';return}
 var statusText=forecast.status==='ready'?'统一模型·含已验证输出':forecast.status==='candidate'?'统一模型候选·观察中':forecast.status==='baseline'?'持续性基线·无需预热':forecast.status==='stale'?'节点数据过期':'等待有效节点数据';
 status.textContent=statusText+' · v'+Number(forecast.model_version||0);
 var points=forecast.points||[],peerCount=Number(forecast.peer_count||0),weather=forecast.weather_used?'已纳入当前网格天气':'当前无可用天气';
 meta.textContent='电脑端与 S3 共用同一模型参数及推理公式；同仓有效参考节点 '+peerCount+' 个，'+weather+'。采用值仅使用已验证输出，其他通道回退持续性基线；模型候选观察值不参与告警或采样控制。';
 host.innerHTML=points.map(function(point){var t=point.temperature_validated?'已验证模型':'持续性基线回退',h=point.rh_validated?'已验证模型':'持续性基线回退';var preview=point.candidate_available?'<span class="candidate-preview">模型候选观察：'+Number(point.candidate_temp).toFixed(1)+' °C / '+Number(point.candidate_rh).toFixed(1)+' %RH · 未验证，不参与控制</span>':'<span class="candidate-preview">统一模型尚未就绪</span>';return '<div><small>+'+Number(point.hour)+' 小时</small><b>采用 '+Number(point.temp).toFixed(1)+' °C</b><span>温度：'+t+'</span><b>'+Number(point.rh).toFixed(1)+' %RH</b><span>湿度：'+h+'</span>'+preview+'</div>'}).join('')||'<span>节点数据陈旧或无效，暂不预测。</span>';
}
/* NODE_INVENTORY_BEGIN */
function renderNodeInventory(rows) {
 if(!Array.isArray(rows)||!rows.length)return '<div class="inventory-empty">暂无有效节点；收到节点数据后会在此列出地址、传感器类型与实测值。</div>';
 return rows.slice(0,8).map(function(row){
  var rawAddr=Number(row.addr),address=isFinite(rawAddr)?'0x'+('0'+rawAddr.toString(16).toUpperCase()).slice(-2):'未知地址';
  var type=String(row.stype||'未知传感器'),role=type==='SHT31'?'温度与相对湿度采集（SHT31，单点）':type==='GENERIC'?'通用传感器节点；具体职责待配置':'传感器类型 '+type+'；具体职责待确认';
  var temp=row.temp===null||row.temp===undefined||!isFinite(Number(row.temp))?'未上报':Number(row.temp).toFixed(2)+' ℃';
  var rh=row.rh===null||row.rh===undefined||!isFinite(Number(row.rh))?'未上报':Number(row.rh).toFixed(2)+' %RH';
  var alarm=Number(row.status)!==0,stale=nodeIsStale(row),state=alarm?'告警':stale?'数据陈旧':'正常';
  return '<article class="inventory-node"><div class="inventory-node-head"><b>'+address+'</b><span class="badge '+((alarm||stale)?'off':'')+'">'+state+'</span></div>'+
   '<p class="inventory-role">'+esc(type)+' · '+esc(role)+' · 最近采样 '+sampleAge(row.sample_age_ms)+'</p><div class="inventory-values">'+
   '<div class="inventory-value"><span>温度</span><strong>'+temp+'</strong></div><div class="inventory-value"><span>相对湿度</span><strong>'+rh+'</strong></div></div></article>';
 }).join('');
}
/* NODE_INVENTORY_END */
/* HOME_NODE_RENDERER_BEGIN */
function renderHomeNodeList(rows) {
 if(!Array.isArray(rows)||!rows.length)return '<div class="home-node-empty">暂无有效节点数据；请先查看顶部连接状态和最近采样时间。</div>';
 return rows.slice(0,8).map(function(row){
  var rawAddr=Number(row.addr),address=isFinite(rawAddr)?'0x'+('0'+rawAddr.toString(16).toUpperCase()).slice(-2):'未知地址';
  var type=esc(String(row.stype||'未知传感器'));
  var temp=row.temp===null||row.temp===undefined||!isFinite(Number(row.temp))?'—':Number(row.temp).toFixed(1)+' °C';
  var rh=row.rh===null||row.rh===undefined||!isFinite(Number(row.rh))?'—':Number(row.rh).toFixed(1)+' %RH';
  var abnormal=Number(row.status||0)!==0,stale=nodeIsStale(row),state=abnormal?'状态异常':stale?'数据陈旧':'正常';
  return '<a class="home-node-row" href="#data" data-route="data" data-home-address="'+(isFinite(rawAddr)?rawAddr:'')+'">'+
   '<span class="home-node-identity"><b>节点 '+address+'</b><small>'+type+' · 地址 '+(isFinite(rawAddr)?rawAddr:'未知')+'</small></span>'+
   '<span class="home-node-value"><small>温度</small><b>'+temp+'</b></span>'+
   '<span class="home-node-value"><small>相对湿度</small><b>'+rh+'</b></span>'+
   '<span class="badge '+((abnormal||stale)?'off':'')+'">'+state+' · '+sampleAge(row.sample_age_ms)+'</span></a>';
 }).join('');
}
/* HOME_NODE_RENDERER_END */
function renderSelected(){
 var node=nodeRows[selectedNode],total=nodeRows.length;
 document.getElementById('nodePage').textContent=total?'节点 '+(selectedNode+1)+' / '+total:'节点 0 / 0';
 document.getElementById('prevNode').disabled=total<2;
 document.getElementById('nextNode').disabled=total<2;
  document.getElementById('allNodeCount').textContent=total;
  var address=node?'0x'+('0'+Number(node.addr).toString(16).toUpperCase()).slice(-2)+'（'+Number(node.addr)+'）':'等待节点';
  document.getElementById('nodeAddr').textContent=address;
  document.getElementById('homeNodes').innerHTML=renderHomeNodeList(nodeRows);
 document.getElementById('sensorType').textContent=node?String(node.stype):'—';
 document.getElementById('nodeSampleTime').textContent=node?sampleAge(node.sample_age_ms):'等待数据';
 document.getElementById('nodeTemp').innerHTML=node?fmt(node.temp,'°C',1):'— <small>°C</small>';
 document.getElementById('nodeRh').innerHTML=node?fmt(node.rh,'%RH',1):'— <small>%RH</small>';
 document.getElementById('tempReading').classList.toggle('empty',!node||node.temp===null);
 document.getElementById('rhReading').classList.toggle('empty',!node||node.rh===null);
 renderNodeForecast(node);
 var table=document.getElementById('nodes');
 table.innerHTML='';
 if(!total)table.innerHTML='<tr><td colspan="5">暂无有效节点；请检查 RS485、节点供电和采样周期。</td></tr>';
 else nodeRows.forEach(function(row){
  var addr=('0'+Number(row.addr).toString(16).toUpperCase()).slice(-2);
  var stale=nodeIsStale(row),alarm=Number(row.status)!==0,state=alarm?'告警':stale?'数据陈旧':'正常';
  table.innerHTML+='<tr><td>0x'+addr+'</td><td>'+esc(row.stype)+'</td><td>'+(row.temp===null?'—':Number(row.temp).toFixed(2)+' ℃')+'</td><td>'+(row.rh===null?'—':Number(row.rh).toFixed(2)+' %RH')+'</td><td><span class="badge '+((alarm||stale)?'off':'')+'">'+state+'</span><small style="display:block;color:#82928c;margin-top:4px">'+sampleAge(row.sample_age_ms)+'</small></td></tr>';
 });
 document.getElementById('aboutNodeCount').textContent=total+' 个有效节点';
 document.getElementById('aboutNodes').innerHTML=renderNodeInventory(nodeRows);
}
function refresh(){
 var failureStage='连接 S3';
 fetch('/api/status?ts='+Date.now(),{cache:'no-store'})
 .then(function(response){failureStage='读取状态接口';if(!response.ok)throw new Error('HTTP '+response.status);return response.text()})
 .then(function(body){
  failureStage='解析状态数据';
  var data=JSON.parse(body);
  if(!data||typeof data!=='object')throw new Error('状态响应不是 JSON 对象');
  failureStage='更新页面';
  var rows=Array.isArray(data.nodes)?data.nodes:[],keepAddr=nodeRows[selectedNode]?nodeRows[selectedNode].addr:null;
  nodeRows=rows;
  if(keepAddr!==null){
   var same=nodeRows.findIndex(function(item){return Number(item.addr)===Number(keepAddr)});
   selectedNode=same>=0?same:Math.min(selectedNode,Math.max(0,nodeRows.length-1));
  }else selectedNode=Math.min(selectedNode,Math.max(0,nodeRows.length-1));
  window.sampleAgeMs=data.sample_age_ms;
  window.sampleIntervalMs=Number(data.sample_interval_ms||data.interval_ms)||10000;
  currentMode=String(data.mode||'debug').toLowerCase();
  var modeLabel=currentMode==='lowpower'?'低功耗模式':'调试模式';
  document.getElementById('runMode').value=currentMode==='lowpower'?'lowpower':'debug';
  document.getElementById('restartMode').value=currentMode==='lowpower'?'lowpower':'debug';
  document.getElementById('settingsModeValue').textContent=modeLabel;
  setConn(true,'S3 在线');
  document.getElementById('deviceUid').textContent=String(data.uid||'—');
  var registered=Number(data.node_count)||0;
  document.getElementById('summaryText').textContent='设备已连接 · 注册 '+registered+' 个节点 · 有效测量 '+rows.length+' 个 · '+(data.station_configured?'电脑中心站已配置':'尚未配置电脑中心站地址');
  document.getElementById('latestAge').textContent=sampleAge(data.sample_age_ms);
  document.getElementById('latestTime').textContent=data.sample_age_ms===null||data.sample_age_ms===undefined?'尚无有效样本':'最近一次节点上报';
  var rssi=Number(data.rssi),wifiConnected=data.wifi_connected===undefined?(isFinite(rssi)&&rssi!==-127):!!data.wifi_connected;
  document.getElementById('signalValue').textContent=wifiConnected?rssi+' dBm':'未连接路由器';
  document.getElementById('signalHint').textContent=wifiConnected?(rssi>-65?'Wi-Fi 信号良好':rssi>-80?'Wi-Fi 信号一般':'Wi-Fi 信号较弱'):'本地配网热点按设备状态提供';
  document.getElementById('settingsWifiState').textContent=(wifiConnected?'已连接路由器':'未连接路由器（本地配网）')+' · '+(data.station_configured?'电脑中心站已配置':'电脑中心站地址未配置');
  document.getElementById('modeValue').textContent=modeLabel;
  var interval=Number(data.interval_ms);
  function intervalLabel(value){return isFinite(value)&&value>0?(value%60000===0?(value/60000)+' 分钟':(value/1000)+' 秒'):'—'}
  var normal=Number(data.normal_interval_ms),fast=Number(data.fast_interval_ms);
  var revision=Number(data.remote_config_revision)||0;
  var actual=isFinite(interval)&&interval>0?'实际 '+intervalLabel(interval):'采样周期以设备设置为准';
  var targets=isFinite(normal)&&normal>0&&isFinite(fast)&&fast>0?' · 常规 '+intervalLabel(normal)+' / 风险 '+intervalLabel(fast)+(revision?' · 中心站配置 v'+revision:'') : '';
  document.getElementById('modeHint').textContent=actual+targets;
  var alarm=!!Number(data.alarm),measurementRisk=!!data.measurement_risk,predictionRisk=!!data.prediction_risk;
  document.getElementById('alarmValue').textContent=alarm?'设备告警':measurementRisk?'温湿度风险':predictionRisk?'趋势预警':'正常';
  document.getElementById('alarmValue').classList.toggle('active',alarm||measurementRisk||predictionRisk);
  document.getElementById('alarmHint').textContent=alarm?'设备报告存在活动告警':measurementRisk?'实测温湿度触及设备阈值；这是监测提示，不是网页故障':predictionRisk?'趋势预测将触及阈值；请结合实测判断':'实测与可用预测均未超阈值';
  renderSelected();
 }).catch(function(error){
  var detail=String(error&&error.message?error.message:error||'未知错误').replace(/\s+/g,' ').slice(0,160);
  setConn(false,failureStage==='连接 S3'?'S3 暂不可达':'状态读取失败');
  document.getElementById('summaryText').textContent='状态读取失败（'+failureStage+'）：'+detail+'。若页面本身仍能打开，请检查状态接口或响应内容；不能仅凭此提示判断为 Wi-Fi 错误。';
 });
}
function showToast(message,error){var el=document.getElementById('toast');el.textContent=message;el.className='toast'+(error?' error':'');el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(function(){el.hidden=true},3800)}
function postForm(form,path,done){fetch(path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(new FormData(form))}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.text()}).then(done).catch(function(){showToast('操作失败，请检查输入和设备连接。',true)})}
document.getElementById('homeNodes').addEventListener('click',function(event){var link=event.target.closest('a[data-home-address]');if(!link)return;var address=Number(link.getAttribute('data-home-address'));var index=nodeRows.findIndex(function(row){return Number(row.addr)===address});if(index>=0){selectedNode=index;renderSelected()}});
document.querySelectorAll('form[action="/api/wifi"]').forEach(function(f){f.addEventListener('submit',function(e){e.preventDefault();postForm(f,'/api/wifi',function(){showToast('网络配置已保存，重启后生效。')})})});document.querySelectorAll('form[action="/api/th"]').forEach(function(f){f.addEventListener('submit',function(e){e.preventDefault();postForm(f,'/api/th',function(){showToast('阈值与采样策略已保存。')})})});document.querySelectorAll('form[action="/api/report"]').forEach(function(f){f.addEventListener('submit',function(e){e.preventDefault();postForm(f,'/api/report',function(){showToast('已请求立即上报，稍后刷新查看。')})})});document.querySelectorAll('form[action="/api/mode"]').forEach(function(f){f.addEventListener('submit',function(e){e.preventDefault();var restart=f.id==='restartForm';if(confirm(restart?'确认重启设备？网页会短暂断开。':'确认切换运行模式并重启设备？网页会短暂断开。'))postForm(f,'/api/mode',function(){showToast(restart?'重启命令已发送，设备正在重新启动。':'切换命令已发送，设备正在重新启动。')})})});document.getElementById('prevNode').addEventListener('click',function(){if(nodeRows.length>1){selectedNode=(selectedNode+nodeRows.length-1)%nodeRows.length;renderSelected()}});document.getElementById('nextNode').addEventListener('click',function(){if(nodeRows.length>1){selectedNode=(selectedNode+1)%nodeRows.length;renderSelected()}});document.getElementById('refreshNow').addEventListener('click',refresh);refresh();setInterval(refresh,3000);
</script>
</body></html>
)html";

void web_init(void)
{
    srv.on("/", HTTP_GET, []() {
        srv.sendHeader("Cache-Control", "no-store");
        srv.sendHeader("Connection", "close");
        srv.send(200, "text/html; charset=utf-8", PAGE_HTML);
    });

    srv.on("/api/status", HTTP_GET, []() {
        srv.sendHeader("Cache-Control", "no-store");
        srv.sendHeader("Connection", "close");
        srv.send(200, "application/json", statusJson());
    });

    srv.on("/api/wifi", HTTP_POST, []() {
        if (net_set_wifi(srv.arg("ssid").c_str(), srv.arg("pass").c_str(),
                         srv.arg("host").c_str())) {
            srv.send(200, "text/plain", "saved, restart to apply");
        } else {
            srv.send(400, "text/plain", "bad params");
        }
    });

    srv.on("/api/mode", HTTP_POST, []() {
        String m = srv.arg("mode");
        if (m == "lowpower") {
            mode_set(MODE_LOW_POWER);
            srv.send(200, "text/plain", "switched to LOWPOWER, restarting...");
            delay(200);
            ESP.restart();
        } else if (m == "debug") {
            mode_set(MODE_DEBUG);
            srv.send(200, "text/plain", "switched to DEBUG, restarting...");
            delay(200);
            ESP.restart();
        } else {
            srv.send(400, "text/plain", "mode=debug|lowpower");
        }
    });

    srv.on("/api/th", HTTP_POST, []() {
        bool ok = true;
        String th = srv.arg("th"), tl = srv.arg("tl"), rh = srv.arg("rh"),
               poll = srv.arg("poll"), fast = srv.arg("fast");
        if (th.length()) ok &= alarm_set("th", (int32_t)(th.toFloat() * 100.0f));
        if (tl.length()) ok &= alarm_set("tl", (int32_t)(tl.toFloat() * 100.0f));
        if (rh.length()) ok &= alarm_set("rh", (int32_t)(rh.toFloat() * 100.0f));
        if (poll.length()) ok &= alarm_set("poll", (int32_t)poll.toInt());
        if (fast.length()) ok &= alarm_set("fast", (int32_t)fast.toInt());
        srv.send(ok ? 200 : 400, "text/plain", ok ? "thresholds saved" : "bad params");
    });

    srv.on("/api/report", HTTP_POST, []() {
        bool ok = reportNow(true);   /* 调试模式网页按钮：保持 WiFi 常开不断线 */
        srv.send(ok ? 202 : 503, "text/plain", ok ? "queued" : "queue unavailable");
    });

    srv.begin();
    Serial.println("[WEB] server started :80");
}

void web_handle(void)
{
    srv.handleClient();
}
