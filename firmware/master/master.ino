/** AutoLink V2.2 SC2 ESP32 master adapter.
 *
 * The portable Master Core owns request construction, active transaction,
 * response validation, registry mutation and timeout/offline policy.  This
 * adapter owns UART, Preferences, entropy and grain-silo presentation.
 */
#include <autolink_master.h>
#include <autolink_persistence.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_timer.h>
#include <math.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include "../generated/grain_silo_profile.h"
#include "../integration/esp32_journal_storage.h"
#include "alarm.h"
#include "mode.h"
#include "net.h"
#include "oled.h"
#include "web.h"

#define PIN_485_TX 17
#define PIN_485_RX 16
#define PIN_485_DE 4
#define TX_SETTLE_US 1200

typedef struct {
    uint16_t version;
    uint64_t network_id;
    uint32_t current_bus_baud;
    uint8_t bus_baud_known;
    uint8_t node_count;
    al_master_node_record_t nodes[GRAIN_SILO_NODE_MAX];
} grain_registry_t;

static_assert(sizeof(grain_registry_t) <= AL_RECORD_PAYLOAD_MAX,
              "Master registry exceeds journal payload capacity");

static al_master_t s_master;
static al_journal_t s_registry_journal;
static Preferences s_preferences;
static uint8_t s_rx[GS_FRAME_MAX_LEN];
static uint16_t s_rx_len;
static al_time_us_t s_rx_last_us;
static bool s_web_enabled;
/* Short input history for shared-model features only; it is not a second model. */
static gs_forecast_history_t s_forecast_history[GRAIN_SILO_NODE_MAX];
static gs_unified_air_reading_t s_unified_weather;
static uint32_t s_unified_weather_received_ms;
static uint32_t s_unified_weather_age_at_receive_ms;
static QueueHandle_t s_report_queue;
static bool s_report_task_ready;
static bool s_adaptive_fast = true;
static uint8_t s_safe_prediction_streak;
static uint64_t s_last_report_ms;
static bool s_has_reported;
static QueueHandle_t s_settings_queue;
static uint32_t s_sample_sequence[GRAIN_SILO_NODE_MAX];
static uint32_t s_sample_boot_id;
static uint32_t s_last_parse_diag_ms;
static uint32_t s_last_response_diag_ms;

#define GRAIN_REPORT_JSON_CAPACITY 4096u
#define GRAIN_FORECAST_MIN_MAX_AGE_MS UINT64_C(180000)

typedef struct {
    char json[GRAIN_REPORT_JSON_CAPACITY];
    bool keep_wifi;
} grain_report_job_t;

uint8_t s_nodeCount;
SnapNode g_snap[GRAIN_SILO_NODE_MAX];
uint32_t s_last_sample_ms;
gs_unified_model_t g_unified_model;
gs_unified_forecast_t g_unified_forecasts[GRAIN_SILO_NODE_MAX];
uint32_t g_sample_interval_ms = 10000u;
uint32_t g_report_interval_ms = 10000u;
bool g_adaptive_fast = true;
bool g_measurement_risk;
bool g_prediction_risk;
uint32_t g_report_queue_replacements;

static uint64_t forecast_max_age_ms(void)
{
    const uint64_t interval_age = (uint64_t)g_sample_interval_ms * 3u;
    return interval_age > GRAIN_FORECAST_MIN_MAX_AGE_MS ?
           interval_age : GRAIN_FORECAST_MIN_MAX_AGE_MS;
}

static int snapshot_index(uint8_t addr)
{
    for (uint8_t i = 0u; i < s_nodeCount; ++i) {
        if (g_snap[i].valid && g_snap[i].addr == addr)
            return (int)i;
    }
    if (s_nodeCount >= GRAIN_SILO_NODE_MAX) return -1;
    return (int)s_nodeCount++;
}

static void record_node_wire_error(uint8_t addr, uint8_t error_code)
{
    int index = -1;
    for (uint8_t i = 0u; i < s_nodeCount; ++i) {
        if (g_snap[i].valid && g_snap[i].addr == addr) {
            index = (int)i;
            break;
        }
    }
    if (index < 0) return;
    SnapNode *node = &g_snap[index];
    node->last_wire_error = error_code;
    if (node->wire_error_count != UINT32_MAX) ++node->wire_error_count;
    node->last_wire_error_at_ms = millis();
}

static bool node_channel_value(const SnapNode *node, uint8_t channel,
                               float *value)
{
    for (uint8_t i = 0u; i < node->nch; ++i) {
        if (node->ch[i].id == channel) {
            *value = (float)node->ch[i].raw / 100.0f;
            return isfinite(*value);
        }
    }
    return false;
}

typedef struct {
    uint16_t schema;
    gs_unified_model_t model;
} grainsilo_unified_model_record_t;

static void flatten_unified_model(const gs_unified_model_t *model,
                                  float values[GS_UF_PARAMETER_COUNT])
{
    size_t cursor = 0u;
    for (uint8_t i = 0u; i < GS_UF_FEATURE_COUNT; ++i)
        values[cursor++] = model->input_mean[i];
    for (uint8_t i = 0u; i < GS_UF_FEATURE_COUNT; ++i)
        values[cursor++] = model->input_scale[i];
    for (uint8_t i = 0u; i < GS_UF_HIDDEN1_COUNT; ++i)
        for (uint8_t j = 0u; j < GS_UF_FEATURE_COUNT; ++j)
            values[cursor++] = model->w1[i][j];
    for (uint8_t i = 0u; i < GS_UF_HIDDEN1_COUNT; ++i)
        values[cursor++] = model->b1[i];
    for (uint8_t i = 0u; i < GS_UF_HIDDEN2_COUNT; ++i)
        for (uint8_t j = 0u; j < GS_UF_HIDDEN1_COUNT; ++j)
            values[cursor++] = model->w2[i][j];
    for (uint8_t i = 0u; i < GS_UF_HIDDEN2_COUNT; ++i)
        values[cursor++] = model->b2[i];
    for (uint8_t i = 0u; i < GS_UF_OUTPUT_COUNT; ++i)
        for (uint8_t j = 0u; j < GS_UF_HIDDEN2_COUNT; ++j)
            values[cursor++] = model->w3[i][j];
    for (uint8_t i = 0u; i < GS_UF_OUTPUT_COUNT; ++i)
        values[cursor++] = model->b3[i];
    for (uint8_t i = 0u; i < GS_UF_OUTPUT_COUNT; ++i)
        values[cursor++] = model->output_scale[i];
}

static bool restore_unified_model(void)
{
    if (s_preferences.getBytesLength("uf_model") !=
        sizeof(grainsilo_unified_model_record_t)) return false;
    grainsilo_unified_model_record_t record;
    memset(&record, 0, sizeof(record));
    if (s_preferences.getBytes("uf_model", &record, sizeof(record)) !=
        sizeof(record) || record.schema != 1u) return false;
    float parameters[GS_UF_PARAMETER_COUNT];
    flatten_unified_model(&record.model, parameters);
    gs_unified_model_t restored;
    if (!gs_unified_model_load(&restored, record.model.version,
            record.model.validated_mask, parameters, GS_UF_PARAMETER_COUNT))
        return false;
    g_unified_model = restored;
    Serial.printf("[FORECAST] restored model v%lu mask=0x%02X from NVS\n",
                  (unsigned long)restored.version,
                  (unsigned)restored.validated_mask);
    return true;
}

static bool persist_unified_model(const gs_unified_model_t *model)
{
    if (model == NULL || model->version == 0u) return false;
    grainsilo_unified_model_record_t record;
    memset(&record, 0, sizeof(record));
    record.schema = 1u;
    record.model = *model;
    return s_preferences.putBytes("uf_model", &record, sizeof(record)) ==
           sizeof(record);
}

static bool unified_forecast_crosses(const gs_unified_forecast_t *forecast,
                                     const gs_air_thresholds_t *thresholds)
{
    if (forecast == NULL || thresholds == NULL ||
        forecast->status != GS_UF_READY) return false;
    for (uint8_t i = 0u; i < GS_UF_HORIZON_COUNT; ++i) {
        const gs_unified_forecast_point_t *point = &forecast->points[i];
        if (!point->valid) continue;
        if (point->temperature_validated &&
            (point->temperature_c < thresholds->temperature_low_c ||
             point->temperature_c > thresholds->temperature_high_c)) return true;
        if (point->rh_validated &&
            ((thresholds->humidity_low_pct > 0.0f &&
              point->relative_humidity_pct < thresholds->humidity_low_pct) ||
             point->relative_humidity_pct > thresholds->humidity_high_pct)) return true;
    }
    return false;
}

static void update_unified_forecast(uint8_t index, uint64_t now_ms)
{
    if (index >= s_nodeCount || index >= GRAIN_SILO_NODE_MAX) return;
    const SnapNode *node = &g_snap[index];
    gs_unified_air_reading_t current = {0};
    float temperature_c, humidity_pct;
    if (node->valid && node->trusted && node->status == 0u &&
        node_channel_value(node, GS_CH_TEMP, &temperature_c) &&
        node_channel_value(node, GS_CH_RH, &humidity_pct)) {
        const uint32_t age_ms = (uint32_t)(millis() - node->sampled_at_ms);
        current.valid = true;
        current.timestamp_ms = age_ms <= now_ms ? now_ms - age_ms : 0u;
        current.temperature_c = temperature_c;
        current.relative_humidity_pct = humidity_pct;
    }

    gs_unified_air_reading_t history[GS_FORECAST_SAMPLE_CAPACITY];
    size_t history_count = 0u;
    const gs_forecast_history_t *source = &s_forecast_history[index];
    const size_t source_count = source->count > GS_FORECAST_SAMPLE_CAPACITY ?
        GS_FORECAST_SAMPLE_CAPACITY : source->count;
    for (size_t i = 0u; i < source_count; ++i) {
        history[history_count].valid = true;
        history[history_count].timestamp_ms = source->samples[i].timestamp_ms;
        history[history_count].temperature_c = source->samples[i].temperature_c;
        history[history_count].relative_humidity_pct =
            source->samples[i].relative_humidity_pct;
        ++history_count;
    }

    gs_unified_air_reading_t peers[GRAIN_SILO_NODE_MAX];
    size_t peer_count = 0u;
    for (uint8_t i = 0u; i < s_nodeCount && peer_count < GRAIN_SILO_NODE_MAX; ++i) {
        const SnapNode *peer = &g_snap[i];
        if (i == index || !peer->valid || !peer->trusted ||
            peer->status != 0u) continue;
        float peer_temp, peer_rh;
        if (!node_channel_value(peer, GS_CH_TEMP, &peer_temp) ||
            !node_channel_value(peer, GS_CH_RH, &peer_rh)) continue;
        const uint32_t age_ms = (uint32_t)(millis() - peer->sampled_at_ms);
        peers[peer_count].valid = true;
        peers[peer_count].timestamp_ms = age_ms <= now_ms ? now_ms - age_ms : 0u;
        peers[peer_count].temperature_c = peer_temp;
        peers[peer_count].relative_humidity_pct = peer_rh;
        ++peer_count;
    }

    gs_unified_air_reading_t weather = s_unified_weather;
    uint64_t weather_age_ms = UINT64_MAX;
    if (weather.valid && s_unified_weather_received_ms != 0u) {
        weather_age_ms = (uint64_t)s_unified_weather_age_at_receive_ms +
            (uint32_t)(millis() - s_unified_weather_received_ms);
        weather.timestamp_ms = weather_age_ms <= now_ms ?
            now_ms - weather_age_ms : 0u;
    } else {
        weather.valid = false;
    }
    gs_unified_forecast_predict(
        &g_unified_model, &current, history, history_count,
        peers, peer_count, &weather, weather_age_ms, now_ms,
        forecast_max_age_ms(), &g_unified_forecasts[index]);
}

static void update_adaptive_policy(uint64_t now_ms)
{
    const alarm_cfg_t *cfg = alarm_get_cfg();
    gs_air_thresholds_t thresholds;
    bool any_node = false;
    bool all_ready = true;
    bool measured_risk = false;
    bool prediction_risk = false;
    thresholds.temperature_high_c = (float)cfg->tempHigh / 100.0f;
    thresholds.temperature_low_c = (float)cfg->tempLow / 100.0f;
    thresholds.humidity_high_pct = (float)cfg->rhHigh / 100.0f;
    thresholds.humidity_low_pct = (float)cfg->rhLow / 100.0f;

    for (uint8_t i = 0u; i < s_nodeCount; ++i) {
        const SnapNode *node = &g_snap[i];
        float temperature_c, humidity_pct;
        if (!node->valid) continue;
        any_node = true;
        const bool have_temp = node_channel_value(node, GS_CH_TEMP,
                                                  &temperature_c);
        const bool have_rh = node_channel_value(node, GS_CH_RH, &humidity_pct);
        const uint64_t sample_age_ms = (uint32_t)(millis() - node->sampled_at_ms);
        if (!node->trusted || !have_temp || !have_rh ||
            sample_age_ms > forecast_max_age_ms()) {
            all_ready = false;
        }
        if (node->trusted && have_temp &&
            (temperature_c > thresholds.temperature_high_c ||
                          temperature_c < thresholds.temperature_low_c))
            measured_risk = true;
        if (node->trusted && have_rh &&
            ((thresholds.humidity_high_pct > 0.0f &&
                         humidity_pct > thresholds.humidity_high_pct) ||
                        (thresholds.humidity_low_pct > 0.0f &&
                         humidity_pct < thresholds.humidity_low_pct)))
            measured_risk = true;
        if (!g_unified_model.version ||
            (g_unified_model.validated_mask & 0x03u) != 0x03u ||
            g_unified_forecasts[i].status != GS_UF_READY) {
            all_ready = false;
        }
        if (unified_forecast_crosses(&g_unified_forecasts[i], &thresholds)) {
            prediction_risk = true;
        }
    }

    g_measurement_risk = measured_risk;
    g_prediction_risk = prediction_risk;
    if (!any_node || !all_ready || measured_risk || prediction_risk ||
        alarm_is_active()) {
        s_adaptive_fast = true;
        s_safe_prediction_streak = 0u;
    } else if (s_safe_prediction_streak < 3u) {
        ++s_safe_prediction_streak;
        if (s_safe_prediction_streak >= 3u) s_adaptive_fast = false;
    }
    g_adaptive_fast = s_adaptive_fast;

    uint32_t normal_ms = cfg->normalIntervalMs;
    uint32_t fast_ms = cfg->fastIntervalMs;
    if (normal_ms < AL_MASTER_SAMPLE_INTERVAL_MIN_MS)
        normal_ms = AL_MASTER_SAMPLE_INTERVAL_MIN_MS;
    if (normal_ms > AL_MASTER_SAMPLE_INTERVAL_MAX_MS)
        normal_ms = AL_MASTER_SAMPLE_INTERVAL_MAX_MS;
    if (fast_ms < AL_MASTER_SAMPLE_INTERVAL_MIN_MS)
        fast_ms = AL_MASTER_SAMPLE_INTERVAL_MIN_MS;
    if (fast_ms > normal_ms) fast_ms = normal_ms;
    g_sample_interval_ms = s_adaptive_fast ? fast_ms : normal_ms;
    g_report_interval_ms = g_sample_interval_ms;
    /* The public minimum equals the unchanged 1 s Core default. */
    static uint32_t applied_interval_ms = AL_MASTER_SAMPLE_INTERVAL_MIN_MS;
    if (applied_interval_ms != g_sample_interval_ms &&
        al_master_set_sample_interval_ms(&s_master, g_sample_interval_ms,
                                        now_ms))
        applied_interval_ms = g_sample_interval_ms;
}

static void cache_sample(uint8_t addr, const gs_sample_t *sample, bool trusted)
{
    const int index = snapshot_index(addr);
    if (index < 0) return;

    SnapNode *node = &g_snap[index];
    const uint8_t last_wire_error = node->last_wire_error;
    const uint32_t wire_error_count = node->wire_error_count;
    const uint32_t last_wire_error_at_ms = node->last_wire_error_at_ms;
    memset(node, 0, sizeof(*node));
    node->addr = addr;
    node->last_wire_error = last_wire_error;
    node->wire_error_count = wire_error_count;
    node->last_wire_error_at_ms = last_wire_error_at_ms;
    node->stype = sample->sensor_type;
    node->status = sample->status;
    node->trusted = trusted;
    for (uint8_t i = 0u; i < sample->channel_count &&
                        i < GS_CODEC_CHANNEL_CAPACITY && i < GS_CH_MAX; ++i) {
        const gs_sample_channel_t *source = &sample->channels[i];
        if (source->value_len != 2u) continue;
        node->ch[node->nch].id = source->id;
        node->ch[node->nch].raw = gs_get_i16be(source->value);
        ++node->nch;
    }
    node->valid = true;
    node->sampled_at_ms = millis();
    ++s_sample_sequence[index];
    if (s_sample_sequence[index] == 0u) ++s_sample_sequence[index];
    node->sample_boot_id = s_sample_boot_id;
    node->sample_seq = s_sample_sequence[index];
    s_last_sample_ms = millis();

    const uint64_t now_ms = (uint64_t)esp_timer_get_time() / 1000u;
    float temperature_c, humidity_pct;
    if (trusted && node->status == 0u &&
        node_channel_value(node, GS_CH_TEMP, &temperature_c) &&
        node_channel_value(node, GS_CH_RH, &humidity_pct)) {
        (void)gs_forecast_add_sample(&s_forecast_history[index], now_ms,
                                     temperature_c, humidity_pct);
    }
    update_unified_forecast((uint8_t)index, now_ms);
    update_adaptive_policy(now_ms);
}

const char *poleUidStr(void)
{
    static char uid[9];
    const uint32_t value = (uint32_t)ESP.getEfuseMac();
    snprintf(uid, sizeof(uid), "%08lX", (unsigned long)value);
    return uid;
}

static int build_snapshot_json(char *buffer, size_t capacity)
{
    int length = snprintf(
        buffer, capacity,
        "{\"pole_uid\":\"%s\",\"fw\":\"2.2.0\",\"mode\":%d,"
        "\"alarm\":%d,\"measurement_risk\":%d,\"prediction_risk\":%d,"
        "\"adaptive_fast\":%d,\"sample_interval_ms\":%lu,"
        "\"report_interval_ms\":%lu,\"report_queue_replacements\":%lu,"
        "\"remote_config_revision\":%lu,\"forecast_model_version\":%lu,"
        "\"sample_boot_id\":%lu,\"bat_mv\":%u,\"ts\":0,\"nodes\":[",
        poleUidStr(), mode_get() == MODE_DEBUG ? 1 : 0,
        alarm_is_active() ? 1 : 0, g_measurement_risk ? 1 : 0,
        g_prediction_risk ? 1 : 0, g_adaptive_fast ? 1 : 0,
        (unsigned long)g_sample_interval_ms,
        (unsigned long)g_report_interval_ms,
        (unsigned long)g_report_queue_replacements,
        (unsigned long)alarm_remote_config_revision(),
        (unsigned long)g_unified_model.version,
        (unsigned long)s_sample_boot_id,
        (unsigned)oled_bus_mv());
    if (length < 0 || (size_t)length >= capacity) return -1;

    bool first = true;
    for (uint8_t i = 0u; i < s_nodeCount; ++i) {
        const SnapNode *node = &g_snap[i];
        if (!node->valid) continue;
        int16_t temperature = 0;
        uint16_t humidity = 0u;
        bool have_temperature = false;
        bool have_humidity = false;
        for (uint8_t k = 0u; k < node->nch && k < GS_CH_MAX; ++k) {
            if (node->ch[k].id == GS_CH_TEMP) {
                temperature = node->ch[k].raw;
                have_temperature = true;
            } else if (node->ch[k].id == GS_CH_RH && node->ch[k].raw >= 0) {
                humidity = (uint16_t)node->ch[k].raw;
                have_humidity = true;
            }
        }
        const int written = snprintf(
            buffer + length, capacity - (size_t)length,
            "%s{\"addr\":%u,\"type\":%u,\"temp\":%s,\"rh\":%s,\"status\":%u,\"sample_boot_id\":%lu,\"sample_seq\":%lu,\"sample_age_ms\":%lu,\"last_wire_error\":%u,\"wire_error_count\":%lu,\"wire_error_age_ms\":%s,\"forecast\":",
            first ? "" : ",", (unsigned)node->addr, (unsigned)node->stype,
            have_temperature ? String(temperature / 100.0f, 2).c_str() : "null",
            have_humidity ? String(humidity / 100.0f, 2).c_str() : "null",
            (unsigned)node->status, (unsigned long)node->sample_boot_id,
            (unsigned long)node->sample_seq,
            (unsigned long)(millis() - node->sampled_at_ms),
            (unsigned)node->last_wire_error,
            (unsigned long)node->wire_error_count,
            node->wire_error_count ?
                String((unsigned long)(millis() - node->last_wire_error_at_ms)).c_str() :
                "null");
        if (written < 0 || (size_t)written >= capacity - (size_t)length)
            return -1;
        length += written;
        const gs_unified_forecast_t *forecast = &g_unified_forecasts[i];
        const char *status = forecast->status == GS_UF_READY ? "ready" :
            forecast->status == GS_UF_CANDIDATE ? "candidate" :
            forecast->status == GS_UF_BASELINE ? "baseline" :
            forecast->status == GS_UF_STALE ? "stale" : "unavailable";
        const int forecast_head = snprintf(
            buffer + length, capacity - (size_t)length,
            "{\"status\":\"%s\",\"model_version\":%lu,"
            "\"validated_mask\":%u,\"peer_count\":%u,\"weather_used\":%s,"
            "\"latest_age_ms\":%lu,\"points\":[",
            status, (unsigned long)forecast->model_version,
            (unsigned)forecast->validated_mask, (unsigned)forecast->peer_count,
            forecast->weather_used ? "true" : "false",
            (unsigned long)(forecast->latest_timestamp_ms <=
                (uint64_t)esp_timer_get_time() / 1000u ?
                (uint64_t)esp_timer_get_time() / 1000u -
                    forecast->latest_timestamp_ms : 0u));
        if (forecast_head < 0 ||
            (size_t)forecast_head >= capacity - (size_t)length) return -1;
        length += forecast_head;
        bool first_forecast_point = true;
        if (forecast->status == GS_UF_READY ||
            forecast->status == GS_UF_CANDIDATE ||
            forecast->status == GS_UF_BASELINE) {
            for (uint8_t point = 0u; point < GS_UF_HORIZON_COUNT; ++point) {
                const gs_unified_forecast_point_t *p = &forecast->points[point];
                if (!p->valid) continue;
                const String t = String(p->temperature_c, 2);
                const String rh = String(p->relative_humidity_pct, 2);
                const int point_len = snprintf(
                    buffer + length, capacity - (size_t)length,
                    "%s{\"hour\":%u,\"temp\":%s,\"rh\":%s,"
                    "\"temperature_validated\":%s,\"rh_validated\":%s}",
                    first_forecast_point ? "" : ",", (unsigned)p->hour,
                    t.c_str(), rh.c_str(),
                    p->temperature_validated ? "true" : "false",
                    p->rh_validated ? "true" : "false");
                if (point_len < 0 ||
                    (size_t)point_len >= capacity - (size_t)length) return -1;
                length += point_len;
                first_forecast_point = false;
            }
        }
        const int forecast_tail = snprintf(
            buffer + length, capacity - (size_t)length, "]}}");
        if (forecast_tail < 0 ||
            (size_t)forecast_tail >= capacity - (size_t)length) return -1;
        length += forecast_tail;
        first = false;
    }
    const int tail = snprintf(buffer + length, capacity - (size_t)length, "]}");
    if (tail < 0 || (size_t)tail >= capacity - (size_t)length) return -1;
    return length + tail;
}

bool reportNow(bool keepWifi)
{
    if (s_report_queue == NULL || !s_report_task_ready) return false;
    grain_report_job_t job;
    if (build_snapshot_json(job.json, sizeof(job.json)) < 0) return false;
    job.keep_wifi = keepWifi;
    if (uxQueueMessagesWaiting(s_report_queue) > 0u)
        ++g_report_queue_replacements;
    return xQueueOverwrite(s_report_queue, &job) == pdPASS;
}

static void report_worker(void *)
{
    grain_report_job_t job;
    for (;;) {
        if (xQueueReceive(s_report_queue, &job, portMAX_DELAY) != pdTRUE)
            continue;
        bool sent = false;
        if (net_connect(8000u)) {
            char command[32] = {0};
            net_remote_settings_t settings = {0};
            sent = net_post_snapshot(job.json, command, sizeof(command), &settings);
            if (sent && s_settings_queue != NULL &&
                (settings.present || settings.forecast_model_present ||
                 settings.forecast_weather_present)) {
                (void)xQueueOverwrite(s_settings_queue, &settings);
            }
            if (!job.keep_wifi) net_disconnect();
        }
        if (!sent) Serial.println("[REPORT] snapshot delivery failed; next scheduled report will retry");
    }
}

static void apply_queued_remote_settings(void)
{
    if (s_settings_queue == NULL) return;
    net_remote_settings_t settings;
    if (xQueueReceive(s_settings_queue, &settings, 0u) != pdTRUE) return;
    if (settings.forecast_weather_present) {
        s_unified_weather.valid = true;
        s_unified_weather.temperature_c = settings.forecast_weather_temperature_c;
        s_unified_weather.relative_humidity_pct = settings.forecast_weather_rh_pct;
        s_unified_weather_age_at_receive_ms =
            settings.forecast_weather_age_sec * 1000u;
        s_unified_weather_received_ms = millis();
    }
    if (settings.forecast_model_present &&
        settings.forecast_model_version != g_unified_model.version) {
        gs_unified_model_t candidate;
        if (gs_unified_model_load(&candidate,
                settings.forecast_model_version,
                settings.forecast_validated_mask,
                settings.forecast_parameters,
                GS_UF_PARAMETER_COUNT)) {
            g_unified_model = candidate;
            if (!persist_unified_model(&candidate))
                Serial.println("[FORECAST] model active in RAM; NVS save failed");
            else
                Serial.printf("[FORECAST] installed shared model v%lu mask=0x%02X\n",
                              (unsigned long)candidate.version,
                              (unsigned)candidate.validated_mask);
            for (uint8_t i = 0u; i < s_nodeCount; ++i)
                update_unified_forecast(i,
                    (uint64_t)esp_timer_get_time() / 1000u);
        } else {
            Serial.printf("[FORECAST] rejected model payload version=%lu\n",
                          (unsigned long)settings.forecast_model_version);
        }
    }
    if (settings.present &&
        settings.revision > alarm_remote_config_revision()) {
        if (!alarm_apply_remote_config(
                settings.revision, settings.temp_high_centi, settings.temp_low_centi,
                settings.rh_high_centi, settings.rh_low_centi,
                settings.normal_interval_sec, settings.fast_interval_sec)) {
            Serial.printf("[CONFIG] rejected remote settings revision=%lu\n",
                          (unsigned long)settings.revision);
        }
    }
    update_adaptive_policy((uint64_t)esp_timer_get_time() / 1000u);
}

static void schedule_periodic_report(uint64_t now_ms)
{
    if (s_nodeCount == 0u) return;
    if (!s_has_reported || now_ms - s_last_report_ms >= g_report_interval_ms) {
        if (reportNow(mode_get() == MODE_DEBUG)) {
            s_last_report_ms = now_ms;
            s_has_reported = true;
        }
    }
}

static bool storage_read(void *, uint8_t slot, void *data, size_t length)
{
    return grainsilo_nvs_journal_read(s_preferences, slot, data, length,
                                      "master0", "master1");
}

static bool storage_write(void *, uint8_t slot, const void *data, size_t length)
{
    return grainsilo_nvs_journal_write(s_preferences, slot, data, length,
                                       "master0", "master1");
}

static bool save_registry(void)
{
    grain_registry_t registry;
    al_master_registry_snapshot_t snapshot;
    memset(&registry, 0, sizeof(registry));
    if (al_master_export_registry(&s_master, &snapshot, registry.nodes,
                                  GRAIN_SILO_NODE_MAX) !=
        AL_MASTER_REGISTRY_EXPORT_OK) return false;
    registry.version = 2u;
    registry.network_id = snapshot.network_id;
    registry.current_bus_baud = snapshot.current_bus_baud;
    registry.bus_baud_known = snapshot.bus_baud_known ? 1u : 0u;
    registry.node_count = snapshot.node_count;
    return al_journal_commit(&s_registry_journal, &registry, sizeof(registry));
}

static bool restore_registry(void)
{
    grain_registry_t registry;
    size_t length = 0u;
    if (!al_journal_load(&s_registry_journal, &registry, sizeof(registry),
                         &length) || length != sizeof(registry) ||
        registry.version != 2u || registry.network_id == 0u ||
        registry.node_count > GRAIN_SILO_NODE_MAX) return false;
    al_master_init(&s_master, registry.network_id);
    if (!al_master_restore_records(&s_master, registry.nodes,
                                   registry.node_count)) return false;
    if (registry.bus_baud_known == 0u ||
        !al_master_restore_bus_baud(&s_master, registry.current_bus_baud))
        al_master_mark_bus_baud_unknown(&s_master);
    return true;
}

static void send_frame(const gs_frame_t *frame)
{
    uint8_t bytes[GS_FRAME_MAX_LEN];
    /* Every physical TX must belong to a transaction prepared by Master Core.
     * This also covers baud-orchestrator frames, whose begin transition is
     * owned inside al_master_baud_next(). */
    if (!al_master_tx_authorized(&s_master, frame)) return;
    uint16_t length = gs_frame_build(bytes, frame);
    if (length == 0u) return;
    digitalWrite(PIN_485_DE, HIGH);
    Serial1.write(bytes, length);
    Serial1.flush();
    al_transport_timestamp_t tx_stop = al_transport_timestamp(
        AL_BUS_EVENT_TX_CRC_STOP,
        (al_time_us_t)esp_timer_get_time(), UINT32_MAX, false);
    (void)al_master_on_tx_complete_timestamp(&s_master, &tx_stop);
    delayMicroseconds(TX_SETTLE_US);
    digitalWrite(PIN_485_DE, LOW);
    /* Never hold the physical bus driven across a flash/NVS transaction. */
    if (al_master_take_registry_dirty(&s_master))
        (void)al_master_on_persist_result(&s_master, save_registry());
}

static void handle_application_sample(const gs_frame_t *frame, bool trusted)
{
    gs_sample_t sample;
    if (gs_parse_sample_v22(frame->data, frame->len, &sample) != GS_PARSE_OK)
        return;
    Serial.printf("[sample] addr=%u type=%u schema=%u channels=%u status=%u trusted=%u\n",
                  frame->addr, sample.sensor_type, sample.schema_rev,
                  sample.channel_count, sample.status, trusted ? 1u : 0u);
    cache_sample(frame->addr, &sample, trusted);
    oled_report_sample(frame->addr, &sample);
}

static void apply_core_event(const al_master_event_t *event)
{
    if (event->kind == AL_MASTER_EVENT_TIMEOUT && event->node_index >= 0)
        Serial.printf("[RS485] transaction-timeout node_index=%d; registered offline nodes are re-probed by Master Core\n",
                      (int)event->node_index);
    if (event->persist_registry) {
        /* Consume the Core dirty edge with the same durable write; otherwise
         * the next unrelated TX would repeat the already-resolved commit. */
        (void)al_master_take_registry_dirty(&s_master);
        (void)al_master_on_persist_result(&s_master, save_registry());
    }
    if (event->kind == AL_MASTER_EVENT_RETRANSMIT) {
        send_frame(&event->tx_frame);
        return;
    }
    if (event->kind == AL_MASTER_EVENT_SET_BAUD ||
        event->kind == AL_MASTER_EVENT_BAUD_RECOVERY) {
        Serial1.updateBaudRate(event->effect_value);
        if (event->kind == AL_MASTER_EVENT_BAUD_RECOVERY)
            al_master_baud_recovery_applied(&s_master);
    }
}

static void process_response(const gs_frame_t *frame)
{
    al_transport_timestamp_t rx_stop = al_transport_timestamp(
        AL_BUS_EVENT_RX_CRC_STOP, s_rx_last_us, UINT32_MAX, false);
    al_master_event_t event = al_master_receive_at_timestamp(
        &s_master, frame, &rx_stop);
    apply_core_event(&event);
    if (event.kind == AL_MASTER_EVENT_ACCEPTED &&
        event.payload_kind == AL_MASTER_PAYLOAD_SAMPLE)
        handle_application_sample(frame, event.payload_trusted);
    if (event.kind == AL_MASTER_EVENT_ACCEPTED && event.wire_error != 0u) {
        record_node_wire_error(frame->addr, event.wire_error);
        const uint32_t now_ms = millis();
        if (s_last_response_diag_ms == 0u ||
            (uint32_t)(now_ms - s_last_response_diag_ms) >= 2000u) {
            Serial.printf("[RS485] node-wire-error addr=0x%02X response_func=0x%02X code=0x%02X seq=%u\n",
                          (unsigned)frame->addr, (unsigned)frame->func,
                          (unsigned)event.wire_error, (unsigned)frame->seq);
            s_last_response_diag_ms = now_ms;
        }
    }
    if (event.kind == AL_MASTER_EVENT_REJECTED) {
        const uint32_t now_ms = millis();
        if (s_last_response_diag_ms == 0u ||
            (uint32_t)(now_ms - s_last_response_diag_ms) >= 2000u) {
            Serial.printf("[RS485] response-rejected addr=0x%02X func=0x%02X seq=%u len=%u\n",
                          (unsigned)frame->addr, (unsigned)frame->func,
                          (unsigned)frame->seq, (unsigned)frame->len);
            s_last_response_diag_ms = now_ms;
        }
    }
}

static void poll_uart(void)
{
    while (Serial1.available() > 0) {
        uint8_t byte = (uint8_t)Serial1.read();
        al_time_us_t now_us = (al_time_us_t)esp_timer_get_time();
        if (s_rx_len == 0u || now_us - s_rx_last_us >=
            (al_time_us_t)GS_TIMING_T_IFG_MS * 1000u)
            s_rx_len = 0u;
        if (s_rx_len < GS_FRAME_MAX_LEN) s_rx[s_rx_len++] = byte;
        s_rx_last_us = now_us;
    }
    if (s_rx_len > 0u && (al_time_us_t)esp_timer_get_time() - s_rx_last_us >=
        (al_time_us_t)GS_TIMING_T_IFG_MS * 1000u) {
        gs_frame_t frame;
        const int parse_status = (int)gs_frame_parse(s_rx, s_rx_len, &frame);
        if (parse_status == GS_PARSE_OK) {
            process_response(&frame);
        } else {
            const uint32_t now_ms = millis();
            if (s_last_parse_diag_ms == 0u ||
                (uint32_t)(now_ms - s_last_parse_diag_ms) >= 2000u) {
                Serial.printf("[RS485] frame-parse-failed status=%d bytes=%u\n",
                              parse_status, (unsigned)s_rx_len);
                s_last_parse_diag_ms = now_ms;
            }
        }
        s_rx_len = 0u;
    }
}

static void schedule_work(void)
{
    gs_frame_t request;
    al_time_us_t now_us = (al_time_us_t)esp_timer_get_time();
    al_master_time_ms_t now = now_us / 1000u;
    al_master_event_t event = al_master_tick_us(&s_master, now_us);
    apply_core_event(&event);
    /* Protocol work ordering, lease verification cadence and fairness are
     * owned by Master Core. The adapter only supplies time/entropy and sends
     * the already-started transaction. */
    if (al_master_next_transmission(&s_master, now, esp_random(), GS_SLOTS,
                                    &request) == AL_MASTER_NEXT_TX_READY)
        send_frame(&request);
}

void setup(void)
{
    al_storage_port_t storage = {NULL, storage_read, storage_write};
    al_master_platform_status_t platform;
    Serial.begin(115200);
    s_sample_boot_id = esp_random();
    if (s_sample_boot_id == 0u) s_sample_boot_id = 1u;
    mode_init();
    alarm_init(mode_get());
    net_init();
    s_preferences.begin("autolink", false);
    al_journal_init(&s_registry_journal, storage);
    (void)restore_unified_model();
    if (!restore_registry()) {
        uint64_t network = ((uint64_t)esp_random() << 32) | esp_random();
        if (network == 0u) network = 1u;
        al_master_init(&s_master, network);
        if (al_master_stage_registry_persistence(&s_master)) {
            (void)al_master_take_registry_dirty(&s_master);
            (void)al_master_on_persist_result(&s_master, save_registry());
        }
    }
    update_adaptive_policy((uint64_t)esp_timer_get_time() / 1000u);
    pinMode(PIN_485_DE, OUTPUT);
    digitalWrite(PIN_485_DE, LOW);
    if (!al_master_platform_status(&s_master, &platform)) return;
    Serial1.begin(platform.active_baud, SERIAL_8N1, PIN_485_RX, PIN_485_TX);
    oled_init();
    s_settings_queue = xQueueCreate(1u, sizeof(net_remote_settings_t));
    s_report_queue = xQueueCreate(1u, sizeof(grain_report_job_t));
    if (s_report_queue != NULL && s_settings_queue != NULL &&
        xTaskCreatePinnedToCore(report_worker, "grain_report", 8192u, NULL,
                                1u, NULL, 0) == pdPASS) {
        s_report_task_ready = true;
    } else {
        Serial.println("[REPORT] asynchronous worker unavailable");
    }
    if (mode_get() == MODE_DEBUG) {
        web_init();
        s_web_enabled = true;
        if (net_connect(10000u))
            Serial.printf("[WEB] open http://%s/\n", WiFi.localIP().toString().c_str());
        else if (net_is_setup_ap())
            Serial.printf("[WEB] setup AP %s, open http://192.168.4.1/\n",
                          net_setup_ap_ssid());
    }
}

void loop(void)
{
    apply_queued_remote_settings();
    /* Service HTTP before the bus scheduler so a browser cannot starve while
     * the single-node polling path is active. */
    if (s_web_enabled) web_handle();
    poll_uart();
    schedule_work();
    schedule_periodic_report((uint64_t)esp_timer_get_time() / 1000u);
    oled_tick();
}
