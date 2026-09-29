#include "grain_silo_forecast.h"

#include <math.h>
#include <string.h>

static const uint8_t kHorizons[GS_FORECAST_HORIZON_COUNT] = {1u, 3u, 6u};
static const float kMinTemperatureC = -40.0f;
static const float kMaxTemperatureC = 125.0f;
static const uint8_t kMinimumSamples = 6u;
static const uint8_t kHistoryMultiplier = 2u;

static float saturation_vapor_pressure_hpa(float temperature_c)
{
    return 6.112f * expf((17.62f * temperature_c) /
                         (243.12f + temperature_c));
}

static float median(float *values, uint16_t count)
{
    for (uint16_t i = 1u; i < count; ++i) {
        const float value = values[i];
        uint16_t j = i;
        while (j > 0u && values[j - 1u] > value) {
            values[j] = values[j - 1u];
            --j;
        }
        values[j] = value;
    }
    if ((count & 1u) != 0u) return values[count / 2u];
    return (values[count / 2u - 1u] + values[count / 2u]) * 0.5f;
}

static float theil_sen_temperature(const gs_forecast_history_t *history,
                                   uint8_t start, uint8_t end, float *slopes)
{
    uint16_t count = 0u;
    for (uint8_t i = start; i < end; ++i) {
        for (uint8_t j = (uint8_t)(i + 1u); j < end; ++j) {
            const uint64_t delta_ms = history->samples[j].timestamp_ms -
                                      history->samples[i].timestamp_ms;
            if (delta_ms == 0u) continue;
            const float elapsed_hours = (float)delta_ms / 3600000.0f;
            slopes[count++] = (history->samples[j].temperature_c -
                               history->samples[i].temperature_c) /
                              elapsed_hours;
        }
    }
    return count == 0u ? 0.0f : median(slopes, count);
}

static float theil_sen_vapor(const gs_forecast_history_t *history,
                             uint8_t start, uint8_t end, float *slopes)
{
    uint16_t count = 0u;
    for (uint8_t i = start; i < end; ++i) {
        const gs_air_sample_t *left = &history->samples[i];
        const float left_vapor = left->relative_humidity_pct *
            saturation_vapor_pressure_hpa(left->temperature_c) / 100.0f;
        for (uint8_t j = (uint8_t)(i + 1u); j < end; ++j) {
            const gs_air_sample_t *right = &history->samples[j];
            const uint64_t delta_ms = right->timestamp_ms -
                                      left->timestamp_ms;
            if (delta_ms == 0u) continue;
            const float right_vapor = right->relative_humidity_pct *
                saturation_vapor_pressure_hpa(right->temperature_c) / 100.0f;
            const float elapsed_hours = (float)delta_ms / 3600000.0f;
            slopes[count++] = (right_vapor - left_vapor) / elapsed_hours;
        }
    }
    return count == 0u ? 0.0f : median(slopes, count);
}

bool gs_forecast_add_sample(gs_forecast_history_t *history,
                            uint64_t timestamp_ms,
                            float temperature_c,
                            float relative_humidity_pct)
{
    if (history == NULL || !isfinite(temperature_c) ||
        !isfinite(relative_humidity_pct) || temperature_c < kMinTemperatureC ||
        temperature_c > kMaxTemperatureC || relative_humidity_pct < 0.0f ||
        relative_humidity_pct > 100.0f) return false;
    if (history->count > 0u) {
        gs_air_sample_t *last = &history->samples[history->count - 1u];
        if (timestamp_ms < last->timestamp_ms) return false;
        if (timestamp_ms == last->timestamp_ms ||
            timestamp_ms - history->bucket_start_ms <
                GS_FORECAST_SAMPLE_PERIOD_MS) {
            last->temperature_c = temperature_c;
            last->relative_humidity_pct = relative_humidity_pct;
            last->timestamp_ms = timestamp_ms;
            return true;
        }
    } else {
        history->bucket_start_ms = timestamp_ms;
    }
    if (history->count == GS_FORECAST_SAMPLE_CAPACITY) {
        memmove(&history->samples[0], &history->samples[1],
                sizeof(history->samples[0]) *
                    (GS_FORECAST_SAMPLE_CAPACITY - 1u));
        --history->count;
    }
    gs_air_sample_t *next = &history->samples[history->count++];
    next->timestamp_ms = timestamp_ms;
    next->temperature_c = temperature_c;
    next->relative_humidity_pct = relative_humidity_pct;
    history->bucket_start_ms = timestamp_ms;
    return true;
}

void gs_forecast_predict(const gs_forecast_history_t *history,
                         uint64_t now_ms,
                         uint64_t max_age_ms,
                         gs_air_forecast_t *out)
{
    if (out == NULL) return;
    memset(out, 0, sizeof(*out));
    out->status = GS_FORECAST_WARMING_UP;
    if (history == NULL || history->count == 0u ||
        history->count > GS_FORECAST_SAMPLE_CAPACITY) return;

    out->sample_count = history->count;
    const gs_air_sample_t *first = &history->samples[0];
    const gs_air_sample_t *last = &history->samples[history->count - 1u];
    out->latest_timestamp_ms = last->timestamp_ms;
    const uint64_t span_ms = last->timestamp_ms - first->timestamp_ms;
    out->span_seconds = (uint32_t)(span_ms / 1000u);
    if (now_ms < last->timestamp_ms ||
        now_ms - last->timestamp_ms > max_age_ms) {
        out->status = GS_FORECAST_STALE;
        return;
    }
    if (history->count < kMinimumSamples) return;

    float slopes[GS_FORECAST_MAX_WINDOW_SAMPLES *
                 (GS_FORECAST_MAX_WINDOW_SAMPLES - 1u) / 2u];

    const float last_vapor = last->relative_humidity_pct *
        saturation_vapor_pressure_hpa(last->temperature_c) / 100.0f;
    const float age_hours = (float)(now_ms - last->timestamp_ms) / 3600000.0f;
    bool any_ready = false;
    for (uint8_t i = 0u; i < GS_FORECAST_HORIZON_COUNT; ++i) {
        gs_air_forecast_point_t *point = &out->points[i];
        uint64_t required_span_ms = (uint64_t)kHorizons[i] *
            kHistoryMultiplier * UINT64_C(3600000);
        const uint64_t minimum_sample_span_ms =
            (uint64_t)(kMinimumSamples - 1u) * GS_FORECAST_SAMPLE_PERIOD_MS;
        if (required_span_ms < minimum_sample_span_ms)
            required_span_ms = minimum_sample_span_ms;
        const uint64_t cutoff_ms = last->timestamp_ms - required_span_ms;
        uint8_t start = 0u;
        uint8_t end = history->count;
        uint8_t window_count;
        uint64_t window_span_ms;
        const float elapsed_hours = age_hours + (float)kHorizons[i];
        while (start < end &&
               history->samples[start].timestamp_ms < cutoff_ms) {
            ++start;
        }
        if (start == end) continue;
        if ((uint8_t)(end - start) > GS_FORECAST_MAX_WINDOW_SAMPLES)
            start = (uint8_t)(end - GS_FORECAST_MAX_WINDOW_SAMPLES);
        window_count = (uint8_t)(end - start);
        window_span_ms = last->timestamp_ms -
                         history->samples[start].timestamp_ms;
        if (window_count < kMinimumSamples ||
            window_span_ms < required_span_ms) continue;

        point->temperature_slope_c_per_hour =
            theil_sen_temperature(history, start, end, slopes);
        point->vapor_pressure_slope_hpa_per_hour =
            theil_sen_vapor(history, start, end, slopes);
        if (!any_ready) {
            out->temperature_slope_c_per_hour =
                point->temperature_slope_c_per_hour;
            out->vapor_pressure_slope_hpa_per_hour =
                point->vapor_pressure_slope_hpa_per_hour;
        }
        any_ready = true;
        point->valid = true;
        float vapor;
        float saturation;
        point->hour = kHorizons[i];
        point->timestamp_ms = now_ms + (uint64_t)kHorizons[i] *
                              UINT64_C(3600000);
        point->temperature_c = last->temperature_c +
            point->temperature_slope_c_per_hour * elapsed_hours;
        if (point->temperature_c < kMinTemperatureC)
            point->temperature_c = kMinTemperatureC;
        if (point->temperature_c > kMaxTemperatureC)
            point->temperature_c = kMaxTemperatureC;
        vapor = last_vapor +
            point->vapor_pressure_slope_hpa_per_hour * elapsed_hours;
        if (vapor < 0.0f) vapor = 0.0f;
        saturation = saturation_vapor_pressure_hpa(point->temperature_c);
        if (vapor > saturation) vapor = saturation;
        point->vapor_pressure_hpa = vapor;
        point->relative_humidity_pct = saturation > 0.0f ?
            (vapor / saturation) * 100.0f : 0.0f;
        if (point->relative_humidity_pct > 100.0f)
            point->relative_humidity_pct = 100.0f;
        point->history_span_seconds = (uint32_t)(window_span_ms / 1000u);
        point->history_sample_count = window_count;
    }
    if (any_ready) out->status = GS_FORECAST_READY;
}

bool gs_forecast_crosses_threshold(const gs_air_forecast_t *forecast,
                                   const gs_air_thresholds_t *thresholds)
{
    if (forecast == NULL || thresholds == NULL ||
        forecast->status != GS_FORECAST_READY) return false;
    for (uint8_t i = 0u; i < GS_FORECAST_HORIZON_COUNT; ++i) {
        const gs_air_forecast_point_t *point = &forecast->points[i];
        if (!point->valid) continue;
        if (point->temperature_c > thresholds->temperature_high_c ||
            point->temperature_c < thresholds->temperature_low_c ||
            (thresholds->humidity_low_pct > 0.0f &&
             point->relative_humidity_pct < thresholds->humidity_low_pct) ||
            point->relative_humidity_pct > thresholds->humidity_high_pct)
            return true;
    }
    return false;
}
