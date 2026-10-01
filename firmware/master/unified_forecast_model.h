#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define GS_UF_FEATURE_COUNT 10u
#define GS_UF_HIDDEN1_COUNT 4u
#define GS_UF_HIDDEN2_COUNT 3u
#define GS_UF_OUTPUT_COUNT 6u
#define GS_UF_PARAMETER_COUNT 109u
#define GS_UF_HORIZON_COUNT 3u
#define GS_UF_MAX_PEERS 8u

typedef struct {
    uint32_t version;
    uint8_t validated_mask;
    float input_mean[GS_UF_FEATURE_COUNT];
    float input_scale[GS_UF_FEATURE_COUNT];
    float w1[GS_UF_HIDDEN1_COUNT][GS_UF_FEATURE_COUNT];
    float b1[GS_UF_HIDDEN1_COUNT];
    float w2[GS_UF_HIDDEN2_COUNT][GS_UF_HIDDEN1_COUNT];
    float b2[GS_UF_HIDDEN2_COUNT];
    float w3[GS_UF_OUTPUT_COUNT][GS_UF_HIDDEN2_COUNT];
    float b3[GS_UF_OUTPUT_COUNT];
    float output_scale[GS_UF_OUTPUT_COUNT];
} gs_unified_model_t;

typedef struct {
    bool valid;
    uint64_t timestamp_ms;
    float temperature_c;
    float relative_humidity_pct;
} gs_unified_air_reading_t;

typedef enum {
    GS_UF_BASELINE = 0,
    GS_UF_CANDIDATE = 1,
    GS_UF_READY = 2,
    GS_UF_STALE = 3,
    GS_UF_UNAVAILABLE = 4
} gs_unified_status_t;

typedef struct {
    uint8_t hour;
    bool valid;
    bool candidate_available;
    bool temperature_validated;
    bool rh_validated;
    float temperature_c;
    float relative_humidity_pct;
    float candidate_temperature_c;
    float candidate_relative_humidity_pct;
} gs_unified_forecast_point_t;

typedef struct {
    gs_unified_status_t status;
    uint32_t model_version;
    uint8_t validated_mask;
    uint64_t latest_timestamp_ms;
    uint8_t peer_count;
    bool weather_used;
    gs_unified_forecast_point_t points[GS_UF_HORIZON_COUNT];
} gs_unified_forecast_t;

void gs_unified_model_clear(gs_unified_model_t *model);
bool gs_unified_model_load(gs_unified_model_t *model, uint32_t version,
                           uint8_t validated_mask, const float *parameters,
                           size_t parameter_count);
void gs_unified_forecast_predict(
    const gs_unified_model_t *model,
    const gs_unified_air_reading_t *current,
    const gs_unified_air_reading_t *history, size_t history_count,
    const gs_unified_air_reading_t *peers, size_t peer_count,
    const gs_unified_air_reading_t *weather, uint64_t weather_age_ms,
    uint64_t now_ms, uint64_t max_age_ms, gs_unified_forecast_t *out);
