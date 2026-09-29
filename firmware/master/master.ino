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
static gs_forecast_history_t s_forecast_history[GRAIN_SILO_NODE_MAX];
static uint64_t s_forecast_last_sample_ms[GRAIN_SILO_NODE_MAX];
static QueueHandle_t s_report_queue;
static bool s_report_task_ready;
static bool s_adaptive_fast = true;
static uint8_t s_safe_prediction_streak;
static uint64_t s_last_report_ms;
static bool s_has_reported;
static QueueHandle_t s_settings_queue;

#define GRAIN_REPORT_JSON_CAPACITY 4096u
#define GRAIN_FORECAST_MIN_MAX_AGE_MS UINT64_C(180000)

typedef struct {
    char json[GRAIN_REPORT_JSON_CAPACITY];
    bool keep_wifi;
} grain_report_job_t;

uint8_t s_nodeCount;
SnapNode g_snap[GRAIN_SILO_NODE_MAX];
uint32_t s_last_sample_ms;
gs_air_forecast_t g_forecasts[GRAIN_SILO_NODE_MAX];
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
        if (!have_temp || !have_rh || s_forecast_last_sample_ms[i] == 0u ||
            now_ms < s_forecast_last_sample_ms[i] ||
            now_ms - s_forecast_last_sample_ms[i] > forecast_max_age_ms()) {
            all_ready = false;
        }
        if (have_temp && (temperature_c > thresholds.temperature_high_c ||
                          temperature_c < thresholds.temperature_low_c))
            measured_risk = true;
        if (have_rh && ((thresholds.humidity_high_pct > 0.0f &&
                         humidity_pct > thresholds.humidity_high_pct) ||
                        (thresholds.humidity_low_pct > 0.0f &&
                         humidity_pct < thresholds.humidity_low_pct)))
            measured_risk = true;
        if (g_forecasts[i].status != GS_FORECAST_READY) {
            all_ready = false;
        } else if (gs_forecast_crosses_threshold(&g_forecasts[i],
                                                  &thresholds)) {
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
    memset(node, 0, sizeof(*node));
    node->addr = addr;
    node->stype = sample->sensor_type;
    node->status = sample->status;
    for (uint8_t i = 0u; i < sample->channel_count &&
                        i < GS_CODEC_CHANNEL_CAPACITY && i < GS_CH_MAX; ++i) {
        const gs_sample_channel_t *source = &sample->channels[i];
        if (source->value_len != 2u) continue;
        node->ch[node->nch].id = source->id;
        node->ch[node->nch].raw = gs_get_i16be(source->value);
        ++node->nch;
    }
    node->valid = true;
    s_last_sample_ms = millis();

    const uint64_t now_ms = (uint64_t)esp_timer_get_time() / 1000u;
    float temperature_c, humidity_pct;
    if (trusted && node_channel_value(node, GS_CH_TEMP, &temperature_c) &&
        node_channel_value(node, GS_CH_RH, &humidity_pct) &&
        gs_forecast_add_sample(&s_forecast_history[index], now_ms,
                               temperature_c, humidity_pct))
        s_forecast_last_sample_ms[index] = now_ms;
    gs_forecast_predict(&s_forecast_history[index], now_ms,
                        forecast_max_age_ms(), &g_forecasts[index]);
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
        "\"remote_config_revision\":%lu,"
        "\"bat_mv\":%u,\"ts\":0,\"nodes\":[",
        poleUidStr(), mode_get() == MODE_DEBUG ? 1 : 0,
        alarm_is_active() ? 1 : 0, g_measurement_risk ? 1 : 0,
        g_prediction_risk ? 1 : 0, g_adaptive_fast ? 1 : 0,
        (unsigned long)g_sample_interval_ms,
        (unsigned long)g_report_interval_ms,
        (unsigned long)g_report_queue_replacements,
        (unsigned long)alarm_remote_config_revision(),
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
            "%s{\"addr\":%u,\"type\":%u,\"temp\":%s,\"rh\":%s,\"status\":%u,\"forecast\":",
            first ? "" : ",", (unsigned)node->addr, (unsigned)node->stype,
            have_temperature ? String(temperature / 100.0f, 2).c_str() : "null",
            have_humidity ? String(humidity / 100.0f, 2).c_str() : "null",
            (unsigned)node->status);
        if (written < 0 || (size_t)written >= capacity - (size_t)length)
            return -1;
        length += written;
        const gs_air_forecast_t *forecast = &g_forecasts[i];
        const char *status = forecast->status == GS_FORECAST_READY ? "ready" :
            forecast->status == GS_FORECAST_STALE ? "stale" : "warming_up";
        const int forecast_head = snprintf(
            buffer + length, capacity - (size_t)length,
            "{\"status\":\"%s\",\"sample_count\":%u,\"span_s\":%lu,"
            "\"temperature_slope_c_per_h\":%s,\"vapor_pressure_slope_hpa_per_h\":%s,\"points\":[",
            status, (unsigned)forecast->sample_count,
            (unsigned long)forecast->span_seconds,
            forecast->status == GS_FORECAST_READY ?
                String(forecast->temperature_slope_c_per_hour, 4).c_str() : "null",
            forecast->status == GS_FORECAST_READY ?
                String(forecast->vapor_pressure_slope_hpa_per_hour, 4).c_str() : "null");
        if (forecast_head < 0 ||
            (size_t)forecast_head >= capacity - (size_t)length) return -1;
        length += forecast_head;
        bool first_forecast_point = true;
        if (forecast->status == GS_FORECAST_READY) {
            for (uint8_t point = 0u; point < GS_FORECAST_HORIZON_COUNT; ++point) {
                const gs_air_forecast_point_t *p = &forecast->points[point];
                if (!p->valid) continue;
                const String t = String(p->temperature_c, 2);
                const String rh = String(p->relative_humidity_pct, 2);
                const int point_len = snprintf(
                    buffer + length, capacity - (size_t)length,
                    "%s{\"hour\":%u,\"temp\":%s,\"rh\":%s,"
                    "\"history_span_s\":%lu,\"sample_count\":%u}",
                    first_forecast_point ? "" : ",", (unsigned)p->hour,
                    t.c_str(), rh.c_str(),
                    (unsigned long)p->history_span_seconds,
                    (unsigned)p->history_sample_count);
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
            if (sent && settings.present && s_settings_queue != NULL) {
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
    if (settings.revision <= alarm_remote_config_revision()) return;
    if (!alarm_apply_remote_config(
            settings.revision, settings.temp_high_centi, settings.temp_low_centi,
            settings.rh_high_centi, settings.rh_low_centi,
            settings.normal_interval_sec, settings.fast_interval_sec)) {
        Serial.printf("[CONFIG] rejected remote settings revision=%lu\n",
                      (unsigned long)settings.revision);
        return;
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
        if (gs_frame_parse(s_rx, s_rx_len, &frame) == GS_PARSE_OK)
            process_response(&frame);
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
    mode_init();
    alarm_init(mode_get());
    net_init();
    s_preferences.begin("autolink", false);
    al_journal_init(&s_registry_journal, storage);
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
