/**
 * mode.cpp - 模式状态机 + NVS 持久化 + BOOT 键切换
 *
 * 切换入口（免重新烧录）：
 *   1. 中心站远程指令: 上报响应 cmd="mode=debug|lowpower" -> mode_set + restart
 *   2. 物理按键: 上电按住 BOOT(GPIO0) 超过 3s -> 强制进调试模式
 *   3. 串口命令: MODE D / MODE L -> mode_set + restart
 *
 * 低功耗模式启动只短检 200ms（省电），按住不放才会继续等到 3s。
 */
#include "mode.h"
#include <Arduino.h>
#include <Preferences.h>

#define NVS_NS       "gs"
#define NVS_KEY_MODE "mode"

#define PIN_BOOT       0
#define KEY_HOLD_MS    3000
#define KEY_POLL_MS    10
#define LP_SNIFF_MS    200   /* 低功耗模式启动仅短检 */

static gs_mode_t s_mode = MODE_DEBUG;

void mode_init(void)
{
    Preferences p;
    p.begin(NVS_NS, true);
    s_mode = (gs_mode_t)p.getUChar(NVS_KEY_MODE, (uint8_t)MODE_DEBUG);
    p.end();

    /* BOOT 键长按检测 */
    bool held = false;
    pinMode(PIN_BOOT, INPUT_PULLUP);
    uint32_t t0 = millis();
    while ((uint32_t)(millis() - t0) < KEY_HOLD_MS) {
        if (digitalRead(PIN_BOOT) == LOW) {
            held = true;
        } else if (held) {
            held = false;                  /* 中途松开，放弃 */
            break;
        }
        if (s_mode == MODE_LOW_POWER &&
            (uint32_t)(millis() - t0) >= LP_SNIFF_MS) {
            break;                         /* 低功耗: 短检不阻塞唤醒流程 */
        }
        delay(KEY_POLL_MS);
    }
    if (held && s_mode != MODE_DEBUG) {
        mode_set(MODE_DEBUG);
    }
    Serial.printf("[MODE] mode=%s key_held=%d\n", mode_name(s_mode), held);
}

gs_mode_t mode_get(void)
{
    return s_mode;
}

void mode_set(gs_mode_t m)
{
    s_mode = m;
    Preferences p;
    p.begin(NVS_NS, false);
    p.putUChar(NVS_KEY_MODE, (uint8_t)m);
    p.end();
    Serial.printf("[MODE] switched to %s\n", mode_name(m));
}

const char *mode_name(gs_mode_t m)
{
    return (m == MODE_DEBUG) ? "DEBUG" : "LOWPOWER";
}
