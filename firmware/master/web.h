#pragma once
/**
 * web.h - 调试模式单杆网页 + 共享快照缓存类型（V1.1）
 *
 * SnapNode / s_nodeCount / g_snap 由 main.ino 定义并填充，
 * web.cpp 只读展示；poleUidStr() / reportNow() 由 main.ino 实现。
 */
#include <stdbool.h>
#include <stdint.h>
#include "gs_proto.h"
#include "grain_silo_forecast.h"
#include "unified_forecast_model.h"

/* 单节点最近一次轮询结果（web 页面 + 快照上报共用） */
typedef struct {
    uint8_t  addr;
    uint8_t  stype;             /* 传感器类型 0x01 SHT31 ... */
    uint8_t  status;            /* 协议状态字节 */
    gs_channel_t ch[GS_CH_MAX];
    uint8_t  nch;
    bool     valid;
    bool     trusted;           /* validated payload from registered node */
    uint32_t sampled_at_ms;     /* 本节点最近一次有效样本的 S3 单调时钟 */
    uint32_t sample_boot_id;    /* 区分 S3 重启前后的样本序号 */
    uint32_t sample_seq;
    uint8_t  last_wire_error;   /* 最近一次已验收的节点协议错误；不属于测量值 */
    uint32_t wire_error_count;  /* 当前 S3 启动周期内累计次数 */
    uint32_t last_wire_error_at_ms;
} SnapNode;

extern uint8_t  s_nodeCount;    /* main.ino 定义 */
extern SnapNode g_snap[];       /* main.ino 定义，NODE_MAX 个 */
extern uint32_t s_last_sample_ms; /* 最近一次有效样本的本地时间 */
extern gs_unified_forecast_t g_unified_forecasts[];
extern gs_unified_model_t g_unified_model;
extern uint32_t g_sample_interval_ms;
extern uint32_t g_report_interval_ms;
extern bool g_adaptive_fast;
extern bool g_measurement_risk;
extern bool g_prediction_risk;
extern uint32_t g_report_queue_replacements;

void web_init(void);            /* 调试模式：启动 WebServer */
void web_handle(void);          /* 每循环调用 */

const char *poleUidStr(void);   /* main.ino: eFuse MAC 低 4 字节，8 位大写 HEX */
bool reportNow(bool keepWifi);   /* 非阻塞：将最新快照放入单槽上报队列 */
