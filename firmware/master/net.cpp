/**
 * net.cpp - WiFi + HTTP POST 上报（V1.1）
 *
 * 发送方式：WiFi 连上后一次性 POST 快照 JSON 到中心站
 *   POST http://<host>:8000/api/v1/snapshot
 *   响应 {"ok":true,"cmd":null|"mode=debug"} -> respCmd 取出 cmd
 * 识别方式：body 内 pole_uid = eFuse MAC 低 4 字节（8 位大写十六进制），
 *   中心站首次见该 uid 自动建档，不依赖杆子 IP。
 */
#include "net.h"
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "net_host_validation.h"

/* ================= 默认连接参数（首次使用或 NVS 无配置时） =================
 * 【请修改：SSID】     -> 现场 WiFi 名
 * 【请修改：密码】     -> 现场 WiFi 密码
 * 【请修改：中心站IP】 -> 粮仓旁电脑的局域网 IP
 * 也可以在调试模式网页里填写并保存（存 NVS，免改代码）
 */
#ifndef AUTOLINK_WIFI_SSID
#define AUTOLINK_WIFI_SSID ""
#endif
#ifndef AUTOLINK_WIFI_PASSWORD
#define AUTOLINK_WIFI_PASSWORD ""
#endif
#ifndef AUTOLINK_SERVER_HOST
#define AUTOLINK_SERVER_HOST ""
#endif

#define NET_DEFAULT_SSID AUTOLINK_WIFI_SSID
#define NET_DEFAULT_PASS AUTOLINK_WIFI_PASSWORD
#define NET_DEFAULT_HOST AUTOLINK_SERVER_HOST

#define NET_PORT       8000
#define NET_CONN_MS    8000
#define NET_POST_MS    4000
#define NET_RETRY      2      /* POST 失败重试次数 */

static const char *json_value(const char *json, const char *key)
{
    char needle[48];
    const int written = snprintf(needle, sizeof(needle), "\"%s\"", key);
    if (written <= 0 || (size_t)written >= sizeof(needle)) return NULL;
    const char *p = strstr(json, needle);
    if (!p) return NULL;
    p = strchr(p + written, ':');
    if (!p) return NULL;
    do { ++p; } while (*p && isspace((unsigned char)*p));
    return p;
}

static bool json_integer(const char *json, const char *key, int64_t *out)
{
    const char *p = json_value(json, key);
    if (!p || !out) return false;
    char *end = NULL;
    const long long value = strtoll(p, &end, 10);
    if (end == p || (*end != ',' && *end != '}' && !isspace((unsigned char)*end)))
        return false;
    *out = (int64_t)value;
    return true;
}

static void parse_response_cmd(const char *json, char *out, size_t capacity)
{
    if (!out || capacity == 0u) return;
    out[0] = '\0';
    const char *p = json_value(json, "cmd");
    if (!p || *p != '"') return;
    ++p;
    size_t n = 0u;
    while (*p && *p != '"' && n + 1u < capacity) {
        if (*p == '\\' && p[1]) ++p;
        out[n++] = *p++;
    }
    out[n] = '\0';
}

static void parse_remote_settings(const char *json, net_remote_settings_t *settings)
{
    if (!settings) return;
    memset(settings, 0, sizeof(*settings));
    int64_t revision, temp_high, temp_low, rh_high, rh_low, normal_sec, fast_sec;
    if (!json_integer(json, "revision", &revision) ||
        !json_integer(json, "temp_high_centi", &temp_high) ||
        !json_integer(json, "temp_low_centi", &temp_low) ||
        !json_integer(json, "rh_high_centi", &rh_high) ||
        !json_integer(json, "rh_low_centi", &rh_low) ||
        !json_integer(json, "normal_interval_sec", &normal_sec) ||
        !json_integer(json, "fast_interval_sec", &fast_sec) ||
        revision <= 0 || revision > UINT32_MAX ||
        temp_high < INT32_MIN || temp_high > INT32_MAX ||
        temp_low < INT32_MIN || temp_low > INT32_MAX ||
        rh_high < INT32_MIN || rh_high > INT32_MAX ||
        rh_low < INT32_MIN || rh_low > INT32_MAX ||
        normal_sec < 0 || normal_sec > UINT32_MAX ||
        fast_sec < 0 || fast_sec > UINT32_MAX) return;
    settings->revision = (uint32_t)revision;
    settings->temp_high_centi = (int32_t)temp_high;
    settings->temp_low_centi = (int32_t)temp_low;
    settings->rh_high_centi = (int32_t)rh_high;
    settings->rh_low_centi = (int32_t)rh_low;
    settings->normal_interval_sec = (uint32_t)normal_sec;
    settings->fast_interval_sec = (uint32_t)fast_sec;
    settings->present = true;
}

static char  s_ssid[33] = NET_DEFAULT_SSID;
static char  s_pass[65] = NET_DEFAULT_PASS;
static char  s_host[40] = NET_DEFAULT_HOST;
static int8_t s_rssi = -127;
static bool s_setup_ap = false;
static char s_setup_ap_ssid[33] = {0};

static void start_setup_ap(void)
{
    if (s_setup_ap) return;
    const uint32_t chip = (uint32_t)ESP.getEfuseMac();
    snprintf(s_setup_ap_ssid, sizeof(s_setup_ap_ssid), "GrainSilo-%04lX",
             (unsigned long)(chip & 0xFFFFu));
    WiFi.mode(WIFI_AP_STA);
    /* Keep the development AP responsive while the browser polls status. */
    WiFi.setSleep(false);
    WiFi.softAPConfig(IPAddress(192, 168, 4, 1),
                      IPAddress(192, 168, 4, 1),
                      IPAddress(255, 255, 255, 0));
    s_setup_ap = WiFi.softAP(s_setup_ap_ssid);
    if (s_setup_ap) {
        Serial.printf("[NET] setup AP=%s ip=%s (development only)\n",
                      s_setup_ap_ssid, WiFi.softAPIP().toString().c_str());
    } else {
        Serial.println("[NET] setup AP start FAILED");
    }
}

void net_init(void)
{
    Preferences p;
    p.begin("gs", true);
    String ssid = p.getString("wifi_ssid", "");
    String pass = p.getString("wifi_pass", "");
    String host = p.getString("wifi_host", "");
    p.end();

    if (ssid.length() > 0) strncpy(s_ssid, ssid.c_str(), sizeof(s_ssid) - 1);
    if (pass.length() > 0) strncpy(s_pass, pass.c_str(), sizeof(s_pass) - 1);
    if (host.length() > 0) strncpy(s_host, host.c_str(), sizeof(s_host) - 1);

    WiFi.mode(WIFI_STA);
    if (s_ssid[0] == '\0') start_setup_ap();
    Serial.printf("[NET] wifi=%s host=%s:%d\n", s_ssid, s_host, NET_PORT);
}

bool net_connect(uint32_t timeoutMs)
{
    if (s_setup_ap) return false;
    if (WiFi.status() == WL_CONNECTED) {
        return true;
    }
    Serial.printf("[NET] connecting to %s ...\n", s_ssid);
    WiFi.begin(s_ssid, s_pass);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED &&
           (uint32_t)(millis() - t0) < timeoutMs) {
        delay(100);
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[NET] connect FAILED");
        start_setup_ap();
        return false;
    }
    s_rssi = WiFi.RSSI();
    Serial.printf("[NET] connected ip=%s rssi=%d dBm\n",
                  WiFi.localIP().toString().c_str(), s_rssi);
    return true;
}

bool net_post_snapshot(const char *body, char *respCmd, size_t respCmdCap,
                       net_remote_settings_t *settings)
{
    if (respCmd && respCmdCap > 0u) respCmd[0] = '\0';
    if (settings) memset(settings, 0, sizeof(*settings));
    if (s_host[0] == '\0') {
        Serial.println("[NET] POST skipped: station host is not configured");
        return false;
    }
    char url[80];
    snprintf(url, sizeof(url), "http://%s:%d/api/v1/snapshot", s_host, NET_PORT);

    for (int retry = 0; retry <= NET_RETRY; retry++) {
        HTTPClient http;
        http.setTimeout(NET_POST_MS);
        http.begin(url);
        http.addHeader("Content-Type", "application/json");
        int code = http.POST(body);
        if (code == 200) {
            String resp = http.getString();
            http.end();
            parse_response_cmd(resp.c_str(), respCmd, respCmdCap);
            parse_remote_settings(resp.c_str(), settings);
            Serial.printf("[NET] POST ok code=%d\n", code);
            return true;
        }
        Serial.printf("[NET] POST failed code=%d (retry %d)\n", code, retry);
        http.end();
        delay(200);
    }
    return false;
}

void net_disconnect(void)
{
    WiFi.disconnect(true);
    if (s_setup_ap) {
        WiFi.mode(WIFI_AP);
        WiFi.softAP(s_setup_ap_ssid);
    } else {
        WiFi.mode(WIFI_OFF);
    }
}

bool net_set_wifi(const char *ssid, const char *pass, const char *host)
{
    if (ssid == NULL || pass == NULL || host == NULL) return false;
    size_t n1 = strlen(ssid), n2 = strlen(pass), n3 = strlen(host);
    if (n1 > 32 || n2 > 64 || n3 > 39 ||
        (n1 == 0 && n2 > 0) || (n1 == 0 && n3 == 0) ||
        (n3 > 0 && !gs_is_valid_ipv4(host))) {
        return false;
    }
    Preferences p;
    p.begin("gs", false);
    if (n1 > 0) p.putString("wifi_ssid", ssid);
    if (n2 > 0) p.putString("wifi_pass", pass);
    if (n3 > 0) p.putString("wifi_host", host);
    p.end();
    return true;
}

int8_t net_rssi(void)
{
    return s_rssi;
}

bool net_wifi_connected(void)
{
    return WiFi.status() == WL_CONNECTED;
}

bool net_station_host_configured(void)
{
    return s_host[0] != '\0';
}

bool net_is_setup_ap(void)
{
    return s_setup_ap;
}

const char *net_setup_ap_ssid(void)
{
    return s_setup_ap ? s_setup_ap_ssid : "";
}
