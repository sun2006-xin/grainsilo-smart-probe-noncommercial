#include "unified_forecast_model.h"

#include <math.h>
#include <string.h>

static const uint8_t kHorizons[GS_UF_HORIZON_COUNT] = {1u, 3u, 6u};
static const float kMinTemperatureC = -40.0f;
static const float kMaxTemperatureC = 125.0f;

static bool finite_value(float value)
{
    return isfinite(value) && fabsf(value) <= 10000.0f;
}

static float saturation_vapor_pressure_hpa(float temperature_c)
{
    const float denominator = 243.12f + temperature_c;
    if (!isfinite(temperature_c) || fabsf(denominator) < 1e-6f) return 0.0f;
    return 6.112f * expf((17.62f * temperature_c) / denominator);
}

void gs_unified_model_clear(gs_unified_model_t *model)
{
    if (model != NULL) memset(model, 0, sizeof(*model));
}

bool gs_unified_model_load(gs_unified_model_t *model, uint32_t version,
                           uint8_t validated_mask, const float *parameters,
                           size_t parameter_count)
{
    if (model == NULL || parameters == NULL || version == 0u ||
        parameter_count != GS_UF_PARAMETER_COUNT ||
        (validated_mask & 0xC0u) != 0u) return false;
    gs_unified_model_t candidate;
    memset(&candidate, 0, sizeof(candidate));
    size_t cursor = 0u;
#define GS_UF_READ(target, count) do { \
    for (size_t gs_uf_i = 0u; gs_uf_i < (count); ++gs_uf_i) { \
        const float gs_uf_value = parameters[cursor++]; \
        if (!finite_value(gs_uf_value)) return false; \
        (target)[gs_uf_i] = gs_uf_value; \
    } \
} while (0)
    GS_UF_READ(candidate.input_mean, GS_UF_FEATURE_COUNT);
    GS_UF_READ(candidate.input_scale, GS_UF_FEATURE_COUNT);
    for (uint8_t row = 0u; row < GS_UF_HIDDEN1_COUNT; ++row)
        GS_UF_READ(candidate.w1[row], GS_UF_FEATURE_COUNT);
    GS_UF_READ(candidate.b1, GS_UF_HIDDEN1_COUNT);
    for (uint8_t row = 0u; row < GS_UF_HIDDEN2_COUNT; ++row)
        GS_UF_READ(candidate.w2[row], GS_UF_HIDDEN1_COUNT);
    GS_UF_READ(candidate.b2, GS_UF_HIDDEN2_COUNT);
    for (uint8_t row = 0u; row < GS_UF_OUTPUT_COUNT; ++row)
        GS_UF_READ(candidate.w3[row], GS_UF_HIDDEN2_COUNT);
    GS_UF_READ(candidate.b3, GS_UF_OUTPUT_COUNT);
    GS_UF_READ(candidate.output_scale, GS_UF_OUTPUT_COUNT);
#undef GS_UF_READ
    if (cursor != parameter_count) return false;
    for (uint8_t i = 0u; i < GS_UF_FEATURE_COUNT; ++i) {
        if (candidate.input_scale[i] <= 1e-8f) return false;
    }
    for (uint8_t i = 0u; i < GS_UF_OUTPUT_COUNT; ++i) {
        if (candidate.output_scale[i] <= 1e-8f) return false;
    }
    candidate.version = version;
    candidate.validated_mask = validated_mask;
    *model = candidate;
    return true;
}

static bool reading_valid(const gs_unified_air_reading_t *reading)
{
    return reading != NULL && reading->valid &&
        isfinite(reading->temperature_c) &&
        reading->temperature_c >= kMinTemperatureC &&
        reading->temperature_c <= kMaxTemperatureC &&
        isfinite(reading->relative_humidity_pct) &&
        reading->relative_humidity_pct >= 0.0f &&
        reading->relative_humidity_pct <= 100.0f;
}

static void forward(const gs_unified_model_t *model, const float *features,
                    float *output)
{
    float a1[GS_UF_HIDDEN1_COUNT];
    float a2[GS_UF_HIDDEN2_COUNT];
    for (uint8_t i = 0u; i < GS_UF_HIDDEN1_COUNT; ++i) {
        float sum = model->b1[i];
        for (uint8_t j = 0u; j < GS_UF_FEATURE_COUNT; ++j)
            sum += model->w1[i][j] * features[j];
        a1[i] = tanhf(sum);
    }
    for (uint8_t i = 0u; i < GS_UF_HIDDEN2_COUNT; ++i) {
        float sum = model->b2[i];
        for (uint8_t j = 0u; j < GS_UF_HIDDEN1_COUNT; ++j)
            sum += model->w2[i][j] * a1[j];
        a2[i] = tanhf(sum);
    }
    for (uint8_t i = 0u; i < GS_UF_OUTPUT_COUNT; ++i) {
        float sum = model->b3[i];
        for (uint8_t j = 0u; j < GS_UF_HIDDEN2_COUNT; ++j)
            sum += model->w3[i][j] * a2[j];
        output[i] = sum * model->output_scale[i];
    }
}

void gs_unified_forecast_predict(
    const gs_unified_model_t *model,
    const gs_unified_air_reading_t *current,
    const gs_unified_air_reading_t *history, size_t history_count,
    const gs_unified_air_reading_t *peers, size_t peer_count,
    const gs_unified_air_reading_t *weather, uint64_t weather_age_ms,
    uint64_t now_ms, uint64_t max_age_ms, gs_unified_forecast_t *out)
{
    if (out == NULL) return;
    memset(out, 0, sizeof(*out));
    out->status = GS_UF_UNAVAILABLE;
    out->model_version = model != NULL ? model->version : 0u;
    out->validated_mask = model != NULL ? model->validated_mask : 0u;
    if (!reading_valid(current)) return;
    out->latest_timestamp_ms = current->timestamp_ms;
    if (now_ms < current->timestamp_ms ||
        now_ms - current->timestamp_ms > max_age_ms) {
        out->status = GS_UF_STALE;
        return;
    }

    float current_vapor = current->relative_humidity_pct *
        saturation_vapor_pressure_hpa(current->temperature_c) / 100.0f;
    if (!isfinite(current_vapor)) return;

    const gs_unified_air_reading_t *previous = NULL;
    uint64_t best_previous_distance = UINT64_MAX;
    const uint64_t previous_target = current->timestamp_ms > 3600000u ?
        current->timestamp_ms - 3600000u : 0u;
    for (size_t i = 0u; i < history_count; ++i) {
        const gs_unified_air_reading_t *candidate = &history[i];
        if (!reading_valid(candidate) ||
            candidate->timestamp_ms >= current->timestamp_ms) continue;
        const uint64_t distance = candidate->timestamp_ms > previous_target ?
            candidate->timestamp_ms - previous_target :
            previous_target - candidate->timestamp_ms;
        if (distance <= 1800000u && distance < best_previous_distance) {
            previous = candidate;
            best_previous_distance = distance;
        }
    }

    float peer_temperature = current->temperature_c;
    float peer_vapor = current_vapor;
    uint8_t included_peers = 0u;
    peer_count = peer_count > GS_UF_MAX_PEERS ? GS_UF_MAX_PEERS : peer_count;
    for (size_t i = 0u; i < peer_count; ++i) {
        const gs_unified_air_reading_t *peer = &peers[i];
        if (!reading_valid(peer) || peer->timestamp_ms > now_ms ||
            now_ms - peer->timestamp_ms > max_age_ms ||
            (peer->timestamp_ms > current->timestamp_ms ?
                peer->timestamp_ms - current->timestamp_ms :
                current->timestamp_ms - peer->timestamp_ms) > 2700000u) continue;
        peer_temperature += peer->temperature_c;
        peer_vapor += peer->relative_humidity_pct *
            saturation_vapor_pressure_hpa(peer->temperature_c) / 100.0f;
        ++included_peers;
    }
    if (included_peers > 0u) {
        peer_temperature = (peer_temperature - current->temperature_c) /
                           included_peers - current->temperature_c;
        peer_vapor = (peer_vapor - current_vapor) / included_peers -
                     current_vapor;
    } else {
        peer_temperature = 0.0f;
        peer_vapor = 0.0f;
    }
    out->peer_count = included_peers;

    bool have_weather = reading_valid(weather) &&
        weather_age_ms <= 10800000u &&
        weather->timestamp_ms <= current->timestamp_ms &&
        current->timestamp_ms - weather->timestamp_ms <= 10800000u;
    out->weather_used = have_weather;
    const float weather_temp = have_weather ? weather->temperature_c : 0.0f;
    const float weather_vapor = have_weather ? weather->relative_humidity_pct *
        saturation_vapor_pressure_hpa(weather->temperature_c) / 100.0f : 0.0f;
    const float features[GS_UF_FEATURE_COUNT] = {
        current->temperature_c, current_vapor,
        previous != NULL ? current->temperature_c - previous->temperature_c : 0.0f,
        previous != NULL ? current_vapor - previous->relative_humidity_pct *
            saturation_vapor_pressure_hpa(previous->temperature_c) / 100.0f : 0.0f,
        peer_temperature, peer_vapor,
        weather_temp, weather_vapor, have_weather ? 1.0f : 0.0f,
        (float)included_peers / 2.0f > 1.0f ? 1.0f : (float)included_peers / 2.0f,
    };
    float output[GS_UF_OUTPUT_COUNT] = {0};
    bool model_valid = model != NULL && model->version > 0u;
    if (model_valid) {
        float normalized[GS_UF_FEATURE_COUNT];
        for (uint8_t i = 0u; i < GS_UF_FEATURE_COUNT; ++i) {
            if (!finite_value(model->input_mean[i]) ||
                !finite_value(model->input_scale[i]) ||
                model->input_scale[i] <= 1e-8f) {
                model_valid = false;
                break;
            }
            normalized[i] = (features[i] - model->input_mean[i]) /
                            model->input_scale[i];
            if (!isfinite(normalized[i])) model_valid = false;
        }
        if (model_valid) {
            forward(model, normalized, output);
            for (uint8_t i = 0u; i < GS_UF_OUTPUT_COUNT; ++i) {
                if (!isfinite(output[i])) model_valid = false;
            }
        }
    }
    if (!model_valid) out->status = GS_UF_BASELINE;
    else if (model->validated_mask == 0u) out->status = GS_UF_CANDIDATE;
    else out->status = GS_UF_READY;

    for (uint8_t i = 0u; i < GS_UF_HORIZON_COUNT; ++i) {
        gs_unified_forecast_point_t *point = &out->points[i];
        const uint8_t temp_bit = (uint8_t)(1u << (i * 2u));
        const uint8_t vapor_bit = (uint8_t)(1u << (i * 2u + 1u));
        point->hour = kHorizons[i];
        point->valid = true;
        point->candidate_available = model_valid;
        point->temperature_validated = model_valid &&
            (model->validated_mask & temp_bit) != 0u;
        point->rh_validated = model_valid &&
            (model->validated_mask & vapor_bit) != 0u;
        float candidate_temperature = current->temperature_c +
            (model_valid ? output[i * 2u] : 0.0f);
        if (candidate_temperature < kMinTemperatureC)
            candidate_temperature = kMinTemperatureC;
        if (candidate_temperature > kMaxTemperatureC)
            candidate_temperature = kMaxTemperatureC;
        float candidate_vapor = current_vapor +
            (model_valid ? output[i * 2u + 1u] : 0.0f);
        if (candidate_vapor < 0.0f) candidate_vapor = 0.0f;
        const float candidate_saturation =
            saturation_vapor_pressure_hpa(candidate_temperature);
        if (candidate_vapor > candidate_saturation)
            candidate_vapor = candidate_saturation;
        point->candidate_temperature_c = candidate_temperature;
        point->candidate_relative_humidity_pct = candidate_saturation > 0.0f ?
            (candidate_vapor / candidate_saturation) * 100.0f : 0.0f;
        float temperature = current->temperature_c;
        float vapor = current_vapor;
        if (point->temperature_validated)
            temperature += output[i * 2u];
        if (point->rh_validated)
            vapor += output[i * 2u + 1u];
        if (temperature < kMinTemperatureC) temperature = kMinTemperatureC;
        if (temperature > kMaxTemperatureC) temperature = kMaxTemperatureC;
        if (vapor < 0.0f) vapor = 0.0f;
        const float saturation = saturation_vapor_pressure_hpa(temperature);
        if (vapor > saturation) vapor = saturation;
        point->temperature_c = temperature;
        point->relative_humidity_pct = saturation > 0.0f ?
            (vapor / saturation) * 100.0f : 0.0f;
        if (!isfinite(point->temperature_c) ||
            !isfinite(point->relative_humidity_pct)) point->valid = false;
    }
}
