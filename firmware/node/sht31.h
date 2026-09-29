/**
 * sht31.h - SHT31 温湿度传感器驱动（I2C，地址 0x44）
 * 独立驱动层，与协议层解耦；后续更换传感器只需替换本文件
 */
#ifndef SHT31_H
#define SHT31_H

#include <Arduino.h>
#include <Wire.h>

class SHT31 {
public:
    static constexpr uint8_t I2C_ADDR = 0x44;

    /** 初始化并检测传感器存在（读 ID 寄存器 0x5449） */
    bool begin();
    /** 单次测量：高重复性，带 CRC8 校验。成功返回 true */
    bool readTempRH(float &tempC, float &rhPct);

private:
    /** SHT3x 专用 CRC8（多项式 0x31，初值 0xFF） */
    static uint8_t crc8(const uint8_t *data, size_t len);
    bool isPresent();
};

#endif /* SHT31_H */
