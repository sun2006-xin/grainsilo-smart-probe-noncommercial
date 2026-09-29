#ifndef GRAIN_OLED_H
#define GRAIN_OLED_H

#include <gs_proto.h>
#include <stdbool.h>
#include <stdint.h>

/* GrainSilo master display layer: 0.96" SSD1306 (128x64, I2C addr 0x3C) and
 * bus battery monitor (ADC1).
 *
 * This is a pure GrainSilo business-layer module. It only consumes data that
 * the AutoLink public API already hands the adapter (accepted sample frames)
 * plus a local ADC reading; the frozen AutoLink v2.2.0 library and the frozen
 * GSProto Wire layer are not modified.
 *
 * Display hardware absence degrades gracefully: every call is a no-op when
 * the panel did not answer at init time, so protocol smoke tests run without
 * the screen attached. */

#define OLED_I2C_SDA   8
#define OLED_I2C_SCL   9
#define OLED_I2C_ADDR  0x3Cu

/* Battery sense pin: ADC1_CH5. Do NOT use ADC2 pins: ESP32 ADC2 is
 * unavailable while Wi-Fi is active. The bus divider is 100k+43k, so the
 * full 2S voltage window (6.0V..8.4V) maps to 1.80V..2.53V at the ADC. */
#define OLED_BAT_GPIO  5u

void oled_init(void);

/* Feed the newest accepted node sample. Takes ownership of nothing; the
 * sample is decoded and copied into the display buffer synchronously. */
void oled_report_sample(uint8_t addr, const gs_sample_t *sample);

/* Periodic refresh; call from the main loop (own cadence, ~500ms). */
void oled_tick(void);

/* Filtered bus voltage estimate in millivolts (0 until first sample). */
uint16_t oled_bus_mv(void);

#endif /* GRAIN_OLED_H */
