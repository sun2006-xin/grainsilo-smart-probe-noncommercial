/**
 * sht31.cpp - SHT31 驱动实现
 * 测量命令 0x2C 0x06（高重复性），响应 6 字节：温度(2)+CRC(1)+湿度(2)+CRC(1)
 * 温度 = -45 + 175 * raw / 65535；湿度 = 100 * raw / 65535
 */
#include "sht31.h"

uint8_t SHT31::crc8(const uint8_t *data, size_t len)
{
    uint8_t crc = 0xFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t b = 0; b < 8; b++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

bool SHT31::isPresent()
{
    uint8_t buf[6];

    /* SHT3x Get Serial Number command: 0x3780.  The response is
     * two 16-bit words, each followed by its CRC8 byte. */
    Wire.beginTransmission(I2C_ADDR);
    Wire.write(0x37);
    Wire.write(0x80);
    if (Wire.endTransmission() != 0) {
        return false;
    }
    delay(1);                   /* tIDLE before the read header */
    if (Wire.requestFrom((uint8_t)I2C_ADDR, (uint8_t)6) != 6) {
        return false;
    }
    for (uint8_t i = 0; i < 6; i++) {
        buf[i] = Wire.read();
    }
    if (crc8(buf, 2) != buf[2] || crc8(buf + 3, 2) != buf[5]) {
        return false;
    }
    return true;
}

bool SHT31::begin()
{
    return isPresent();
}

bool SHT31::readTempRH(float &tempC, float &rhPct)
{
    uint8_t buf[6];
    uint16_t tRaw, hRaw;

    Wire.beginTransmission(I2C_ADDR);
    Wire.write(0x2C);           /* 单次测量，高重复性，时钟拉伸 */
    Wire.write(0x06);
    if (Wire.endTransmission() != 0) {
        return false;
    }
    delay(20);                  /* 典型 15ms，留余量 */
    if (Wire.requestFrom((uint8_t)I2C_ADDR, (uint8_t)6) != 6) {
        return false;
    }
    for (uint8_t i = 0; i < 6; i++) {
        buf[i] = Wire.read();
    }
    if (crc8(buf, 2) != buf[2] || crc8(buf + 3, 2) != buf[5]) {
        return false;           /* 温度或湿度 CRC 校验失败 */
    }
    tRaw = ((uint16_t)buf[0] << 8) | buf[1];
    hRaw = ((uint16_t)buf[3] << 8) | buf[4];
    tempC = -45.0f + 175.0f * (float)tRaw / 65535.0f;
    rhPct = 100.0f * (float)hRaw / 65535.0f;
    return true;
}
