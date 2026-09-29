/**
 * alarm.cpp - 阈值判断 + 周期切换（15min <-> 1min）+ NVS 持久化
 *
 * 状态机（每轮采集结束调用 alarm_check 一次，不是每节点一次）：
 *   NORMAL --连续 triggerCnt 轮有任一节点任一通道超限--> FAST
 *   FAST   --连续 recoverCnt 轮全部正常--> NORMAL
 * 掉线节点不参与判断（数据缺失不算超限也不算正常）。
 * 掉电重启：NVS alarm 标志恢复 FAST 状态，不会静默退回慢周期。
 * 阈值存 NVS + CRC8（多项式 0x31），校验失败回退默认值。
 */
#include "alarm.h"
#include <Arduino.h>
#include <Preferences.h>
#include <string.h>

#define NVS_NS       "gs"
#define NVS_KEY_CFG  "alarm_cfg"
#define NVS_KEY_FLAG "alarm"
#define NVS_KEY_REMOTE_REV "remote_rev"

static alarm_cfg_t s_cfg = {
    3000,     /* tempHigh  30.00C */
    -500,     /* tempLow   -5.00C */
    7000,     /* rhHigh    70.00% */
    0,        /* rhLow     不启用 */
    900000,   /* normal    15min */
    60000,    /* fast      1min */
    2,        /* triggerCnt */
    3         /* recoverCnt */
};

static bool     s_active = false;
static uint8_t  s_overStreak = 0;
static uint8_t  s_normStreak = 0;
static uint32_t s_remote_config_revision = 0u;

static uint8_t crc8(const uint8_t *d, size_t n)
{
    uint8_t c = 0;
    for (size_t i = 0; i < n; i++) {
        c ^= d[i];
        for (uint8_t b = 0; b < 8; b++) {
            c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x31) : (uint8_t)(c << 1);
        }
    }
    return c;
}

void alarm_init(gs_mode_t mode)
{
    Preferences p;
    p.begin(NVS_NS, true);

    s_active = (p.getUChar(NVS_KEY_FLAG, 0) != 0);
    s_remote_config_revision = p.getUInt(NVS_KEY_REMOTE_REV, 0u);

    size_t len = p.getBytesLength(NVS_KEY_CFG);
    if (len == sizeof(alarm_cfg_t) + 1) {
        uint8_t buf[sizeof(alarm_cfg_t) + 1];
        p.getBytes(NVS_KEY_CFG, buf, sizeof(buf));
        if (crc8(buf, sizeof(alarm_cfg_t)) == buf[sizeof(alarm_cfg_t)]) {
            memcpy(&s_cfg, buf, sizeof(alarm_cfg_t));
        }
    } else if (mode == MODE_DEBUG) {
        /* 首次无配置：常规每分钟，风险时每 10 秒；仅 RAM 默认，不写 NVS。 */
        s_cfg.normalIntervalMs = 60000;
        s_cfg.fastIntervalMs = 10000;
    }
    p.end();

    Serial.printf("[ALARM] active=%d th=%d.%02d tl=%d.%02d rh=%d.%02d poll=%lus fast=%lus\n",
                  s_active,
                  (int)(s_cfg.tempHigh / 100), (int)(s_cfg.tempHigh % 100 < 0 ? -s_cfg.tempHigh % 100 : s_cfg.tempHigh % 100),
                  (int)(s_cfg.tempLow / 100),  (int)(s_cfg.tempLow % 100 < 0 ? -s_cfg.tempLow % 100 : s_cfg.tempLow % 100),
                  (int)(s_cfg.rhHigh / 100),   (int)(s_cfg.rhHigh % 100),
                  (unsigned long)(s_cfg.normalIntervalMs / 1000),
                  (unsigned long)(s_cfg.fastIntervalMs / 1000));
}

bool alarm_is_over(const gs_channel_t *ch, uint8_t nch)
{
    for (uint8_t i = 0; i < nch; i++) {
        switch (ch[i].id) {
        case GS_CH_TEMP:
            if (ch[i].raw > s_cfg.tempHigh || ch[i].raw < s_cfg.tempLow) {
                return true;
            }
            break;
        case GS_CH_RH:
            if (s_cfg.rhHigh > 0 && ch[i].raw > s_cfg.rhHigh) {
                return true;
            }
            if (s_cfg.rhLow > 0 && ch[i].raw < s_cfg.rhLow) {
                return true;
            }
            break;
        default:
            break;
        }
    }
    return false;
}

void alarm_check(bool anyOver)
{
    if (anyOver) {
        s_normStreak = 0;
        if (++s_overStreak >= s_cfg.triggerCnt) {
            if (!s_active) {
                s_active = true;
                Preferences p;
                p.begin(NVS_NS, false);
                p.putUChar(NVS_KEY_FLAG, 1);
                p.end();
                Serial.printf("[ALARM] TRIGGERED -> fast interval (%lus)\n",
                              (unsigned long)(s_cfg.fastIntervalMs / 1000));
            }
        }
    } else {
        s_overStreak = 0;
        if (s_active) {
            if (++s_normStreak >= s_cfg.recoverCnt) {
                s_active = false;
                Preferences p;
                p.begin(NVS_NS, false);
                p.putUChar(NVS_KEY_FLAG, 0);
                p.end();
                Serial.println("[ALARM] RECOVERED -> normal interval");
            }
        }
    }
}

bool alarm_is_active(void)
{
    return s_active;
}

uint32_t alarm_get_interval_ms(void)
{
    return s_active ? s_cfg.fastIntervalMs : s_cfg.normalIntervalMs;
}

bool alarm_set(const char *key, int32_t v)
{
    if (strcmp(key, "th") == 0) {
        if (v < -4000 || v > 8000) return false;      /* -40.00..80.00C */
        s_cfg.tempHigh = v;
    } else if (strcmp(key, "tl") == 0) {
        if (v < -4000 || v > 8000) return false;
        s_cfg.tempLow = v;
    } else if (strcmp(key, "rh") == 0) {
        if (v < 0 || v > 10000) return false;         /* 0..100.00%RH */
        s_cfg.rhHigh = v;
    } else if (strcmp(key, "rl") == 0) {
        if (v < 0 || v > 10000) return false;
        s_cfg.rhLow = v;
    } else if (strcmp(key, "poll") == 0) {
        if (v < 1 || v > 86400) return false;
        s_cfg.normalIntervalMs = (uint32_t)v * 1000u;
    } else if (strcmp(key, "fast") == 0) {
        if (v < 5 || v > 3600) return false;
        s_cfg.fastIntervalMs = (uint32_t)v * 1000u;
    } else {
        return false;
    }

    uint8_t buf[sizeof(alarm_cfg_t) + 1];
    memcpy(buf, &s_cfg, sizeof(alarm_cfg_t));
    buf[sizeof(alarm_cfg_t)] = crc8(buf, sizeof(alarm_cfg_t));
    Preferences p;
    p.begin(NVS_NS, false);
    p.putBytes(NVS_KEY_CFG, buf, sizeof(buf));
    p.end();
    return true;
}

bool alarm_apply_remote_config(uint32_t revision, int32_t temp_high_centi,
                               int32_t temp_low_centi, int32_t rh_high_centi,
                               int32_t rh_low_centi,
                               uint32_t normal_interval_sec,
                               uint32_t fast_interval_sec)
{
    if (revision == 0u) return false;
    if (revision == s_remote_config_revision) return true;
    if (revision < s_remote_config_revision) return false;
    if (temp_low_centi < -4000 || temp_high_centi > 8000 ||
        temp_low_centi >= temp_high_centi || rh_low_centi < 0 ||
        rh_high_centi > 10000 || rh_low_centi >= rh_high_centi ||
        normal_interval_sec < 1u || normal_interval_sec > 86400u ||
        fast_interval_sec < 5u || fast_interval_sec > 3600u ||
        fast_interval_sec > normal_interval_sec) return false;

    alarm_cfg_t next = s_cfg;
    next.tempHigh = temp_high_centi;
    next.tempLow = temp_low_centi;
    next.rhHigh = rh_high_centi;
    next.rhLow = rh_low_centi;
    next.normalIntervalMs = normal_interval_sec * 1000u;
    next.fastIntervalMs = fast_interval_sec * 1000u;

    uint8_t buf[sizeof(alarm_cfg_t) + 1];
    memcpy(buf, &next, sizeof(alarm_cfg_t));
    buf[sizeof(alarm_cfg_t)] = crc8(buf, sizeof(alarm_cfg_t));
    Preferences p;
    if (!p.begin(NVS_NS, false)) return false;
    const size_t written = p.putBytes(NVS_KEY_CFG, buf, sizeof(buf));
    p.end();
    if (written != sizeof(buf)) return false;

    /* Config bytes are durable before the revision marker. If power is lost
       between writes, Station retries the same idempotent revision on next POST. */
    s_cfg = next;
    if (!p.begin(NVS_NS, false)) return false;
    const size_t revision_written = p.putUInt(NVS_KEY_REMOTE_REV, revision);
    p.end();
    if (revision_written != sizeof(revision)) return false;

    s_remote_config_revision = revision;
    Serial.printf("[ALARM] remote config applied revision=%lu poll=%lus fast=%lus\n",
                  (unsigned long)revision,
                  (unsigned long)normal_interval_sec,
                  (unsigned long)fast_interval_sec);
    return true;
}

uint32_t alarm_remote_config_revision(void)
{
    return s_remote_config_revision;
}

const alarm_cfg_t *alarm_get_cfg(void)
{
    return &s_cfg;
}
