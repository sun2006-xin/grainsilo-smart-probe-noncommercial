#pragma once
/**
 * mode.h - 单固件双模式（V1.1）
 * MODE_DEBUG     调试模式: WiFi 常开 + 单杆网页（联调/演示）
 * MODE_LOW_POWER 低功耗模式: Deep Sleep 循环，仅上报瞬间开 WiFi
 */
#include <stdint.h>
#include <stdbool.h>

typedef enum {
    MODE_LOW_POWER = 0,
    MODE_DEBUG     = 1
} gs_mode_t;

/* 读 NVS 模式 + BOOT 键长按检测（按住 >3s 强制进调试模式） */
void mode_init(void);

gs_mode_t mode_get(void);

/* 写入 NVS 并生效（调用方按需重启） */
void mode_set(gs_mode_t m);

const char *mode_name(gs_mode_t m);
