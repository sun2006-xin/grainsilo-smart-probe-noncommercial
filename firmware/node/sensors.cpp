/**
 * sensors.cpp - 传感器驱动表与自动识别实现
 *
 * 驱动表结构：probe 为快速指纹检查，read 为数据读取。
 * 当前已录入：SHT31（I2C 0x44，Get Serial Number 命令 0x3780）
 * 预留槽位：CO2（SGP30/SCD40 类，I2C）、粮食水分（ADC 型）——接入方法见 sensors.h
 */
#include "sensors.h"
#include "sht31.h"

/* ================ SHT31 驱动 ================ */
static SHT31 sht;

static bool sht31_probe(void)
{
    return sht.begin();            /* 读序列号两组数据 + CRC8，约 1ms */
}

static bool sht31_read(gs_channel_t *ch, uint8_t *nch, uint8_t *status)
{
    float t, h;
    if (!sht.readTempRH(t, h)) {
        *status |= GS_STATUS_SENSOR_ERR;
        return false;
    }
    ch[0].id  = GS_CH_TEMP;
    ch[0].raw = (int16_t)(t * 100.0f + (t >= 0 ? 0.5f : -0.5f));   /* 0.01°C */
    ch[1].id  = GS_CH_RH;
    ch[1].raw = (int16_t)(h * 100.0f + 0.5f);                       /* 0.01%RH */
    *nch = 2;
    return true;
}

/* ================ 预留：CO2 驱动（接入示例见注释） ================ */
#if 0
/* 例：SGP30 类 I2C CO2 传感器（地址 0x58）
   probe: 读 0x20 0x00 特征字
   read:  读 0x20 0x08 返回 CO2(2B)+TVOC(2B)。Wire STYPE 必须保持
          GS_SENSOR_GENERIC；CH_ID 是 node-local，语义由 descriptor 的
          QUANTITY_ID=CO2_CONCENTRATION 与 DATA_TYPE/UNIT 定义。 */
static bool co2_probe(void)
{
    /* 实现：I2C 读特征字比对 */
    return false;
}
static bool co2_read(gs_channel_t *ch, uint8_t *nch, uint8_t *status)
{
    uint16_t ppm = 0;
    /* 实现：读 CO2 浓度 */
    ch[0].id  = 3; /* node-local ID；必须同时发布对应 descriptor */
    ch[0].raw = (int16_t)ppm;
    *nch = 1;
    return true;
}
#endif

/* ================ 预留：粮食水分驱动（ADC 电阻式） ================ */
#if 0
static bool moisture_probe(void)
{
    /* 实现：ADC 引脚阻抗检测（高阻=未接） */
    return false;
}
static bool moisture_read(gs_channel_t *ch, uint8_t *nch, uint8_t *status)
{
    uint16_t m = 0;
    /* 实现：ADC 采样 -> 标定曲线 -> 水分 */
    ch[0].id  = 4; /* node-local ID；必须同时发布对应 descriptor */
    ch[0].raw = (int16_t)m;
    *nch = 1;
    return true;
}
#endif

/* ================ 驱动表（探测顺序 = 优先匹配顺序） ================ */
const gs_sensor_driver_t gs_sensors[] = {
    { GS_SENSOR_SHT31, "SHT31", sht31_probe, sht31_read },
    /* 接入新传感器：在下方添加条目，如
    { GS_SENSOR_GENERIC, "CO2", co2_probe, co2_read },
    { GS_SENSOR_GENERIC, "MOISTURE", moisture_probe, moisture_read }, */
};
const uint8_t gs_sensor_count = sizeof(gs_sensors) / sizeof(gs_sensors[0]);

static uint8_t s_currentType = GS_SENSOR_GENERIC;

uint8_t sensor_current_type(void)
{
    return s_currentType;
}

uint8_t sensor_auto_detect(void)
{
    s_currentType = GS_SENSOR_GENERIC;
    for (uint8_t i = 0; i < gs_sensor_count; i++) {
        if (gs_sensors[i].probe()) {
            s_currentType = gs_sensors[i].type;
            break;
        }
    }
    return s_currentType;
}

bool sensor_read(gs_channel_t *ch, uint8_t *nch, uint8_t *status)
{
    for (uint8_t i = 0; i < gs_sensor_count; i++) {
        if (gs_sensors[i].type == s_currentType) {
            return gs_sensors[i].read(ch, nch, status);
        }
    }
    return false;
}
