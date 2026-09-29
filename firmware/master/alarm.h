#pragma once
/**
 * alarm.h - 两级告警之一级：杆端超限加速上报（V1.1）
 * NORMAL(15min) --连续 triggerCnt 轮超限--> FAST(1min)
 * FAST(1min)    --连续 recoverCnt 轮正常--> NORMAL(15min)
 */
#include <stdint.h>
#include <stdbool.h>
#include "gs_proto.h"
#include "mode.h"

typedef struct {
    int32_t  tempHigh;        /* 0.01C，默认 3000 = 30.00C */
    int32_t  tempLow;         /* 0.01C，默认 -500 = -5.00C */
    int32_t  rhHigh;          /* 0.01%RH，默认 7000 = 70%RH，0=不启用 */
    int32_t  rhLow;           /* 0.01%RH，默认 0 = 不启用 */
    uint32_t normalIntervalMs; /* 常规采集/上报周期；LOWPOWER 默认 900000 */
    uint32_t fastIntervalMs;   /* 风险采集/上报周期；LOWPOWER 默认 60000 */
    uint8_t  triggerCnt;       /* 连续超限轮数 -> 进入 FAST，默认 2 */
    uint8_t  recoverCnt;       /* 连续正常轮数 -> 恢复 NORMAL，默认 3 */
} alarm_cfg_t;

/* 无有效 NVS 配置时：DEBUG 为 60s/10s，LOWPOWER 为 15min/1min。 */
void alarm_init(gs_mode_t mode);

/* 本轮数据是否超限（主控每成功轮询一轮后对汇总数据调用） */
bool alarm_is_over(const gs_channel_t *ch, uint8_t nch);

/* 每轮采集结束调用一次，更新 FAST/NORMAL 状态机 */
void alarm_check(bool anyOver);

bool alarm_is_active(void);

uint32_t alarm_get_interval_ms(void);

/* key: "th"|"tl"|"rh"|"rl" 单位 0.01C/0.01%RH；"poll"|"fast" 单位秒。存 NVS+CRC8 */
bool alarm_set(const char *key, int32_t v);

/* Apply one validated station configuration revision atomically and persist it. */
bool alarm_apply_remote_config(uint32_t revision, int32_t temp_high_centi,
                               int32_t temp_low_centi, int32_t rh_high_centi,
                               int32_t rh_low_centi,
                               uint32_t normal_interval_sec,
                               uint32_t fast_interval_sec);
uint32_t alarm_remote_config_revision(void);

const alarm_cfg_t *alarm_get_cfg(void);
