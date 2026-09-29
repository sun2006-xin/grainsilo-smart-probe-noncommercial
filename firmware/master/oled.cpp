/* GrainSilo master display layer implementation.
 *
 * Single-page big-number UI on 128x64:
 *
 *   y0  |01 ● 7.93V        ▮▮▮▮ 85%|   addr, comm-fresh dot, bus voltage,
 *   y12 |  (temp icon) 25.4 c        |   battery icon + percent
 *   y36 |  (drop icon)  68%RH        |
 *
 * Values shown: newest accepted node sample (temperature / relative
 * humidity), node address, comm-fresh indicator (<2.5s since last accepted
 * sample), battery percent + bar (two-segment 2S Li-ion map), filtered bus
 * voltage in mV.
 */
#include "oled.h"

#include <Arduino.h>
#include <Adafruit_SSD1306.h>
#include <Wire.h>

#define OLED_W 128
#define OLED_H 64
#define OLED_REFRESH_MS 500u
#define OLED_PROBE_MS 2000u
#define OLED_I2C_CLOCK_HZ 100000u
#define BAT_SAMPLE_MS 1000u

/* 2S divider (100k + 43k): bus = adc * 143 / 43 = adc * 3.3256 */
#define BAT_DIV_NUM 143u
#define BAT_DIV_DEN 43u

#define BAT_FULL_MV 8400u   /* 4.20 V per cell */
#define BAT_HALF_MV 7400u   /* 3.70 V per cell */
#define BAT_EMPTY_MV 6000u  /* 3.00 V per cell */
#define COMM_FRESH_MS 2500u

static Adafruit_SSD1306 s_display(OLED_W, OLED_H, &Wire);
static bool s_ok = false;
static bool s_panel_seen = false;

/* ---- display state ---- */
static bool s_have = false;
static uint8_t s_addr = 0u;
static int16_t s_temp_x100 = 0; /* deg C * 100, sign-magnitude */
static uint16_t s_rh_x100 = 0;  /* %RH * 100 */
static uint32_t s_last_comm_ms = 0u;

/* ---- battery state ---- */
static uint32_t s_bus_mv = 0u; /* IIR-filtered */
static bool s_bat_valid = false;
static uint32_t s_last_bat_ms = 0u;
static uint32_t s_last_refresh_ms = 0u;
static uint32_t s_last_probe_ms = 0u;

static uint32_t now_ms(void)
{
    return (uint32_t)millis();
}

static bool panel_responds(void)
{
    Wire.beginTransmission(OLED_I2C_ADDR);
    return Wire.endTransmission() == 0u;
}

static bool init_panel(void)
{
    if (!panel_responds()) return false;

    /* Wire is already started on the board-specific SDA/SCL pins above. */
    if (!s_display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR, true, false))
        return false;

    s_display.clearDisplay();
    s_display.display();
    return true;
}

/* ------------------------------- battery ------------------------------- */

static uint8_t percent_from_mv(uint32_t mv)
{
    if (mv >= BAT_FULL_MV) return 100u;
    if (mv <= BAT_EMPTY_MV) return 0u;
    if (mv > BAT_HALF_MV) {
        /* 7400..8400 mV -> 50..100 % */
        return (uint8_t)(50u + ((mv - BAT_HALF_MV) * 50u) /
                                 (BAT_FULL_MV - BAT_HALF_MV));
    }
    /* 6000..7400 mV -> 0..50 % */
    return (uint8_t)(((mv - BAT_EMPTY_MV) * 50u) /
                     (BAT_HALF_MV - BAT_EMPTY_MV));
}

static void sample_battery(void)
{
    uint32_t adc_mv = (uint32_t)analogReadMilliVolts(OLED_BAT_GPIO);
    uint32_t bus = (adc_mv * BAT_DIV_NUM) / BAT_DIV_DEN;
    if (!s_bat_valid) {
        s_bus_mv = bus;
        s_bat_valid = true;
    } else {
        s_bus_mv = (s_bus_mv * 7u + bus) / 8u; /* IIR, ~8 samples time const */
    }
}

/* -------------------------------- icons -------------------------------- */

static void icon_temperature(int16_t x, int16_t y)
{
    /* stem + round bulb, 16x18 box */
    s_display.fillRect(x + 7, y + 1, 2, 10, SSD1306_WHITE);
    s_display.fillCircle(x + 8, y + 14, 5, SSD1306_WHITE);
}

static void icon_droplet(int16_t x, int16_t y)
{
    /* 16x18 droplet: triangle tip over round base */
    s_display.fillTriangle(x + 8, y + 1, x + 1, y + 11, x + 15, y + 11,
                           SSD1306_WHITE);
    s_display.fillCircle(x + 8, y + 14, 6, SSD1306_WHITE);
}

static void draw_battery(int16_t x, int16_t y, uint8_t percent)
{
    /* outline 22x10 + positive nub, 4 inner bars by 25% steps */
    s_display.drawRect(x, y, 22, 10, SSD1306_WHITE);
    s_display.fillRect(x + 22, y + 3, 3, 4, SSD1306_WHITE);
    uint8_t bars = (uint8_t)((percent + 12u) / 25u);
    if (bars > 4u) bars = 4u;
    for (uint8_t i = 0u; i < bars; ++i)
        s_display.fillRect(x + 2 + (int16_t)i * 5, y + 2, 3, 6,
                           SSD1306_WHITE);
    if (percent < 5u)
        s_display.drawRect(x + 8, y + 3, 6, 4, SSD1306_WHITE); /* empty mark */
}

static void draw_deg_circle(int16_t cx, int16_t cy, uint8_t r)
{
    s_display.fillCircle(cx, cy, r, SSD1306_WHITE);
}

/* ------------------------------- drawing ------------------------------- */

static void fmt_2_1(char *out, size_t cap, int32_t value_x100)
{
    /* "25.4" / "-5.0"; value is degrees*100 */
    bool neg = value_x100 < 0;
    uint32_t absv = neg ? (uint32_t)(-value_x100) : (uint32_t)value_x100;
    int snprintf_ret = snprintf(out, cap, "%s%lu.%lu",
                                neg ? "-" : "",
                                (unsigned long)(absv / 100u),
                                (unsigned long)((absv % 100u) / 10u));
    (void)snprintf_ret;
}

static void render(void)
{
    s_display.clearDisplay();
    uint8_t percent = s_bat_valid
                          ? percent_from_mv(s_bus_mv)
                          : 0u;

    /* --- top line: addr, comm dot, bus voltage, battery icon --- */
    s_display.setTextSize(1);
    s_display.setTextColor(SSD1306_WHITE);
    char top[16];
    snprintf(top, sizeof(top), "%02u", (unsigned)s_addr);
    s_display.setCursor(0, 1);
    s_display.print(s_have ? top : "--");

    bool fresh = s_have && (now_ms() - s_last_comm_ms) < COMM_FRESH_MS;
    if (fresh)
        s_display.fillCircle(22, 5, 2, SSD1306_WHITE);
    else
        s_display.drawCircle(22, 5, 2, SSD1306_WHITE);

    if (s_bat_valid) {
        snprintf(top, sizeof(top), "%lu.%02luV",
                 (unsigned long)(s_bus_mv / 1000u),
                 (unsigned long)((s_bus_mv % 1000u) / 10u));
        s_display.setCursor(32, 1);
        s_display.print(top);
    }
    char pct[8];
    snprintf(pct, sizeof(pct), "%u%%", (unsigned)percent);
    s_display.setCursor(76, 1);
    s_display.print(pct);
    draw_battery(98, 1, percent);

    /* --- big value rows (icon 16px at x=2, text centered in x24..128) --- */
    if (s_have) {
        /* temperature row */
        icon_temperature(2, 14);
        char txt[12];
        fmt_2_1(txt, sizeof(txt), s_temp_x100);
        int16_t x1 = 0, y1 = 0;
        uint16_t w1 = 0, h1 = 0;
        s_display.setTextSize(3);
        s_display.getTextBounds(txt, 0, 0, &x1, &y1, &w1, &h1);
        int16_t tx = (int16_t)(24 + (128 - 24 - (int16_t)w1) / 2);
        s_display.setCursor(tx, 10);
        s_display.print(txt);
        draw_deg_circle((int16_t)(tx + (int16_t)w1 + 8), 10 + (int16_t)h1 / 2,
                        3);
        s_display.setTextSize(1);
        s_display.setCursor((int16_t)(tx + (int16_t)w1 + 13), 10 + 6);
        s_display.print("C");

        /* humidity row */
        icon_droplet(2, 38);
        int16_t rx = 0, ry = 0;
        uint16_t rw = 0, rh = 0;
        s_display.setTextSize(3);
        snprintf(txt, sizeof(txt), "%u%%", (unsigned)(s_rh_x100 / 100u));
        s_display.getTextBounds(txt, 0, 0, &rx, &ry, &rw, &rh);
        tx = (int16_t)(24 + (128 - 24 - (int16_t)rw) / 2);
        s_display.setCursor(tx, 34);
        s_display.print(txt);
        s_display.setTextSize(1);
        s_display.setCursor((int16_t)(tx + (int16_t)rw + 4), 34 + 7);
        s_display.print("RH");
    } else {
        /* no data yet: big placeholder */
        s_display.setTextSize(3);
        s_display.setCursor(24, 16);
        s_display.print("--.-");
        s_display.setCursor(24, 40);
        s_display.print("--%");
    }
    s_display.display();
}

/* -------------------------------- public -------------------------------- */

void oled_init(void)
{
    Wire.begin(OLED_I2C_SDA, OLED_I2C_SCL);
    Wire.setClock(OLED_I2C_CLOCK_HZ);
    Wire.setTimeOut(25u);
    analogSetPinAttenuation(OLED_BAT_GPIO, ADC_11db);
    s_last_probe_ms = now_ms();
    s_ok = init_panel();
    s_panel_seen = s_ok;
    if (s_ok) {
        Serial.println("[OLED] SSD1306 ready at 0x3C");
    } else {
        Serial.println("[OLED] panel unavailable/init failed at 0x3C; retrying every 2 s");
    }
    s_last_refresh_ms = now_ms();
}

void oled_report_sample(uint8_t addr, const gs_sample_t *sample)
{
    bool got_temp = false;
    bool got_rh = false;
    for (uint8_t i = 0u; i < sample->channel_count &&
                        i < GS_CODEC_CHANNEL_CAPACITY; ++i) {
        const gs_sample_channel_t *ch = &sample->channels[i];
        if (ch->value_len != 2u) continue;
        int16_t raw = (int16_t)((uint16_t)ch->value[0] << 8 | ch->value[1]);
        if (ch->id == GS_CH_TEMP) {
            s_temp_x100 = raw; /* 0.01 deg C */
            got_temp = true;
        } else if (ch->id == GS_CH_RH) {
            s_rh_x100 = raw > 0 ? (uint16_t)raw : 0u; /* 0.01 %RH */
            got_rh = true;
        }
    }
    if (got_temp || got_rh) {
        s_addr = addr;
        s_have = true;
        s_last_comm_ms = now_ms();
    }
}

void oled_tick(void)
{
    uint32_t ms = now_ms();
    if (ms - s_last_bat_ms >= BAT_SAMPLE_MS) {
        s_last_bat_ms = ms;
        sample_battery();
    }

    if (ms - s_last_probe_ms >= OLED_PROBE_MS) {
        s_last_probe_ms = ms;
        bool present = panel_responds();
        if (!present && s_ok) {
            s_ok = false;
            Serial.println("[OLED] I2C display lost; waiting for recovery");
        } else if (present && !s_ok) {
            s_ok = init_panel();
            if (s_ok) {
                Serial.printf("[OLED] display %s and reinitialized\n",
                              s_panel_seen ? "recovered" : "detected");
                s_panel_seen = true;
                s_last_refresh_ms = ms;
                render();
            }
        }
    }

    if (!s_ok) return;
    if (ms - s_last_refresh_ms >= OLED_REFRESH_MS) {
        s_last_refresh_ms = ms;
        render();
    }
}

uint16_t oled_bus_mv(void)
{
    if (!s_bat_valid) return 0u;
    return (uint16_t)(s_bus_mv > 0xFFFFu ? 0xFFFFu : s_bus_mv);
}
