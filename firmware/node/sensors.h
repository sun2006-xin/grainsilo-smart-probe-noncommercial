/**
 * sensors.h - 传感器驱动抽象层（节点自动识别核心）
 *
 * 机制：驱动表 + 指纹探测。每个传感器驱动实现统一接口：
 *   probe() 快速存在性检查（I2C 读 ID / 特征字节，几 ms 内完成）
 *   read()  读取当前测量值，填充通道列表（GS_CH_*）
 *
 * 接入新传感器步骤（选型录入硬件指纹）：
 *   1. 确定 I2C 地址与识别特征（ID 寄存器值或特征字节）
 *   2. 实现 probe()/read()
 *   3. 把驱动加入 gs_sensors[] 驱动表
 * 之后节点自动识别，主机零改动。
 */
#ifndef SENSORS_H
#define SENSORS_H

#include <Arduino.h>
#include <gs_proto.h>

typedef struct {
    uint8_t type;                       /* GS_SENSOR_* */
    const char *name;
    bool (*probe)(void);                /* 快速存在性检查 */
    bool (*read)(gs_channel_t *ch, uint8_t *nch, uint8_t *status); /* 读通道 */
} gs_sensor_driver_t;

/** 遍历驱动表；未识别时返回冻结的 GS_SENSOR_GENERIC。 */
uint8_t sensor_auto_detect(void);

/** 读取当前传感器数据到通道数组，返回 true 成功 */
bool sensor_read(gs_channel_t *ch, uint8_t *nch, uint8_t *status);

/** 当前识别的传感器类型（上次 sensor_auto_detect 结果） */
uint8_t sensor_current_type(void);

/** 驱动表（外部可见，便于扩展） */
extern const gs_sensor_driver_t gs_sensors[];
extern const uint8_t gs_sensor_count;

#endif /* SENSORS_H */
