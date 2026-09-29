#pragma once

#include <stdbool.h>
#include <stdint.h>

/* 50 half-hour points retain almost 25 h of probe-adjacent air history. */
#define GS_FORECAST_SAMPLE_CAPACITY 50u
#define GS_FORECAST_SAMPLE_PERIOD_MS UINT64_C(1800000)
#define GS_FORECAST_MAX_WINDOW_SAMPLES 25u
#define GS_FORECAST_HORIZON_COUNT 3u

typedef enum {
    GS_FORECAST_WARMING_UP,
    GS_FORECAST_STALE,
    GS_FORECAST_READY
} gs_forecast_status_t;

typedef struct {
    uint64_t timestamp_ms;
    float temperature_c;
    float relative_humidity_pct;
} gs_air_sample_t;

typedef struct {
    uint8_t count;
    uint64_t bucket_start_ms;
    gs_air_sample_t samples[GS_FORECAST_SAMPLE_CAPACITY];
} gs_forecast_history_t;

typedef struct {
    uint8_t hour;
    bool valid;
    uint64_t timestamp_ms;
    float temperature_c;
    float relative_humidity_pct;
    float vapor_pressure_hpa;
    float temperature_slope_c_per_hour;
    float vapor_pressure_slope_hpa_per_hour;
    uint32_t history_span_seconds;
    uint8_t history_sample_count;
} gs_air_forecast_point_t;

typedef struct {
    gs_forecast_status_t status;
    uint8_t sample_count;
    uint32_t span_seconds;
    uint64_t latest_timestamp_ms;
    float temperature_slope_c_per_hour;
    float vapor_pressure_slope_hpa_per_hour;
    gs_air_forecast_point_t points[GS_FORECAST_HORIZON_COUNT];
} gs_air_forecast_t;

typedef struct {
    float temperature_low_c;
    float temperature_high_c;
    float humidity_low_pct; /* 0 disables low-RH threshold */
    float humidity_high_pct;
} gs_air_thresholds_t;

bool gs_forecast_add_sample(gs_forecast_history_t *history,
                            uint64_t timestamp_ms,
                            float temperature_c,
                            float relative_humidity_pct);
void gs_forecast_predict(const gs_forecast_history_t *history,
                         uint64_t now_ms,
                         uint64_t max_age_ms,
                         gs_air_forecast_t *out);
bool gs_forecast_crosses_threshold(const gs_air_forecast_t *forecast,
                                   const gs_air_thresholds_t *thresholds);
