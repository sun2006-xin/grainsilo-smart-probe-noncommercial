#pragma once
/**
 * net.h - WiFi 管理 + HTTP POST 快照上报 + 响应 cmd 解析（V1.1）
 * 传输方式：HTTP POST JSON 到中心站 FastAPI，收到 200 即完成，随即断开 WiFi。
 * 识别方式：快照 JSON 携带 pole_uid（eFuse MAC 低 4 字节），中心站首次见 uid 建档。
 */
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

typedef struct {
    bool present;
    uint32_t revision;
    int32_t temp_high_centi;
    int32_t temp_low_centi;
    int32_t rh_high_centi;
    int32_t rh_low_centi;
    uint32_t normal_interval_sec;
    uint32_t fast_interval_sec;
} net_remote_settings_t;

/* 读 NVS WiFi 配置（无配置用编译默认值） */
void net_init(void);

/* 连接 WiFi；返回是否成功（循环内喂看门狗） */
bool net_connect(uint32_t timeoutMs);

/* POST 快照；读取一次性 cmd 与可重试的版本化设备设置。 */
bool net_post_snapshot(const char *body, char *respCmd, size_t respCmdCap,
                       net_remote_settings_t *settings);

void net_disconnect(void);

/* 存 NVS（重启生效） */
bool net_set_wifi(const char *ssid, const char *pass, const char *host);

int8_t net_rssi(void);
bool net_wifi_connected(void);
bool net_station_host_configured(void);

/* Development-only setup AP state; no station credentials are exposed. */
bool net_is_setup_ap(void);
const char *net_setup_ap_ssid(void);
