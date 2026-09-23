#include "echolot_ld2460.h"

#include <cmath>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace echolot_ld2460 {

namespace proto = ::ld2460_protocol;

static const char *const TAG = "echolot_ld2460";

// A report is at most 31 bytes and the module sends a handful per second;
// 512 per pass is several frames of headroom while keeping one loop()
// from starving Wi-Fi and the API if the line fills with noise.
static const size_t READ_BUDGET = 512;
// Seconds after boot before the first version query: the module needs a
// moment after power-up, and the ESP usually boots faster than it.
static const uint32_t VERSION_QUERY_DELAY_MS = 2000;
static const uint32_t VERSION_QUERY_RETRY_MS = 5000;
static const uint8_t VERSION_QUERY_ATTEMPTS = 3;
// Even with nothing new to say, the frame line is refreshed this often,
// so Echolot can tell a silent device from a lost connection.
static const uint32_t FRAME_HEARTBEAT_MS = 1000;
static const uint32_t FIRST_DIAGNOSTICS_MS = 10000;

static const char *state_name(LinkState state) {
  switch (state) {
    case LinkState::RECEIVING:
      return "receiving";
    case LinkState::QUIET:
      return "quiet";
    default:
      return "unknown";
  }
}

void EcholotLd2460::setup() {
  // Nothing to configure on the module. Reporting is on from the factory
  // and everything this component needs is in those reports.
}

void EcholotLd2460::loop() {
  const uint32_t now = millis();
  this->read_uart_(now);

  // An acknowledgement is proof for one probe interval plus the grace
  // the reports themselves get. Longer, and a module unplugged after its
  // last answer would read as "quiet" for far too long.
  const LinkState state =
      proto::classify(this->clock_, now, this->stale_ms_, this->probe_interval_ms_ + this->stale_ms_);

  this->maybe_probe_(now, state);

  if (!this->version_known_ && this->version_queries_ < VERSION_QUERY_ATTEMPTS && now >= VERSION_QUERY_DELAY_MS &&
      (this->version_queries_ == 0 || now - this->last_version_query_ms_ >= VERSION_QUERY_RETRY_MS)) {
    this->write_array(proto::COMMAND_QUERY_VERSION, sizeof(proto::COMMAND_QUERY_VERSION));
    this->version_queries_++;
    this->last_version_query_ms_ = now;
  }

  this->publish_state_(state);
  this->publish_frame_(now, state);

  const bool diagnostics_due = this->last_diagnostics_ms_ == 0
                                   ? now >= FIRST_DIAGNOSTICS_MS
                                   : now - this->last_diagnostics_ms_ >= this->diagnostics_interval_ms_;
  if (diagnostics_due) {
    this->publish_diagnostics_();
    // Never 0 again after the first time, so the branch above stays honest
    // even if the first publish happens to land on millis() == 0 after a wrap.
    this->last_diagnostics_ms_ = now == 0 ? 1 : now;
  }
}

void EcholotLd2460::read_uart_(uint32_t now) {
  uint8_t buffer[64];
  size_t budget = READ_BUDGET;
  size_t available = this->available();
  while (available > 0 && budget > 0) {
    size_t chunk = available < sizeof(buffer) ? available : sizeof(buffer);
    if (chunk > budget)
      chunk = budget;
    if (!this->read_array(buffer, chunk))
      break;
    available -= chunk;
    budget -= chunk;
    this->rx_bytes_ += chunk;
    for (size_t i = 0; i < chunk; ++i) {
      switch (this->parser_.feed(buffer[i])) {
        case proto::Event::REPORT:
          this->latest_ = this->parser_.report();
          this->reports_++;
          if (this->latest_.count == 0)
            this->empty_reports_++;
          this->clock_.have_report = true;
          this->clock_.last_report_ms = now;
          break;
        case proto::Event::ACK:
          this->handle_ack_(now);
          break;
        case proto::Event::NONE:
          break;
      }
    }
  }
}

void EcholotLd2460::handle_ack_(uint32_t now) {
  const proto::Ack &ack = this->parser_.ack();
  if (ack.function == proto::FUNCTION_REPORTING && ack.payload_length >= 1) {
    const uint8_t result = ack.payload[0];
    this->clock_.have_ack = true;
    this->clock_.last_ack_ms = now;
    this->clock_.reporting_enabled = (result & 0x01) != 0;
    if ((result & 0x10) == 0 || (result & 0x01) == 0)
      ESP_LOGW(TAG, "Module did not confirm reporting is on (result 0x%02X)", result);
    return;
  }
  if (ack.function == proto::FUNCTION_VERSION && ack.payload_length >= 5) {
    this->version_known_ = true;
    char text[32];
    snprintf(text, sizeof(text), "V%u.%u (20%02u-%02u)", ack.payload[3], ack.payload[4], ack.payload[1],
             ack.payload[2]);
    ESP_LOGI(TAG, "Module firmware %s", text);
    if (this->firmware_sensor_ != nullptr)
      this->firmware_sensor_->publish_state(text);
    return;
  }
  ESP_LOGD(TAG, "Acknowledgement for function 0x%02X, %u payload byte(s)", ack.function, ack.payload_length);
}

void EcholotLd2460::maybe_probe_(uint32_t now, LinkState state) {
  if (state == LinkState::RECEIVING)
    return;
  if (this->probe_sent_ && now - this->last_probe_ms_ < this->probe_interval_ms_)
    return;
  this->write_array(proto::COMMAND_ENABLE_REPORTING, sizeof(proto::COMMAND_ENABLE_REPORTING));
  this->probe_sent_ = true;
  this->last_probe_ms_ = now;
}

void EcholotLd2460::publish_state_(LinkState state) {
  if (!this->state_published_ || state != this->published_state_) {
    ESP_LOGD(TAG, "Link %s", state_name(state));
    if (this->status_sensor_ != nullptr)
      this->status_sensor_->publish_state(state_name(state));
    this->published_state_ = state;
    this->state_published_ = true;
  }
  if (this->count_sensor_ == nullptr)
    return;
  float count = NAN;
  if (state == LinkState::RECEIVING) {
    count = this->latest_.count;
  } else if (state == LinkState::QUIET && this->quiet_means_empty_) {
    count = 0;
  }
  const bool same = (std::isnan(count) && std::isnan(this->published_count_)) || count == this->published_count_;
  if (!same) {
    this->count_sensor_->publish_state(count);
    this->published_count_ = count;
  }
}

void EcholotLd2460::publish_frame_(uint32_t now, LinkState state) {
  if (this->frame_sensor_ == nullptr)
    return;
  const uint32_t since = now - this->last_frame_publish_ms_;
  const bool fresh_report = this->reports_ != this->published_seq_;
  const bool due = !this->frame_published_ || state != this->frame_state_ || since >= FRAME_HEARTBEAT_MS ||
                   (fresh_report && since >= this->frame_interval_ms_);
  if (!due)
    return;
  char text[proto::FRAME_TEXT_CAPACITY];
  if (proto::encode_frame(text, sizeof(text), state, this->reports_, this->latest_) == 0)
    return;
  this->frame_sensor_->publish_state(text);
  this->published_seq_ = this->reports_;
  this->frame_state_ = state;
  this->frame_published_ = true;
  this->last_frame_publish_ms_ = now;
}

void EcholotLd2460::publish_diagnostics_() {
  if (this->rx_bytes_sensor_ != nullptr)
    this->rx_bytes_sensor_->publish_state(this->rx_bytes_);
  if (this->reports_sensor_ != nullptr)
    this->reports_sensor_->publish_state(this->reports_);
  if (this->empty_sensor_ != nullptr)
    this->empty_sensor_->publish_state(this->empty_reports_);
  if (this->rejected_sensor_ != nullptr)
    this->rejected_sensor_->publish_state(this->parser_.rejected);
}

void EcholotLd2460::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Echolot LD2460:\n"
                "  Stale after: %" PRIu32 " ms\n"
                "  Probe interval: %" PRIu32 " ms\n"
                "  Frame interval: %" PRIu32 " ms\n"
                "  Quiet means empty: %s",
                this->stale_ms_, this->probe_interval_ms_, this->frame_interval_ms_,
                YESNO(this->quiet_means_empty_));
  this->check_uart_settings(115200);
  LOG_TEXT_SENSOR("  ", "Frame", this->frame_sensor_);
  LOG_TEXT_SENSOR("  ", "Status", this->status_sensor_);
  LOG_TEXT_SENSOR("  ", "Radar firmware", this->firmware_sensor_);
  LOG_SENSOR("  ", "Targets", this->count_sensor_);
}

}  // namespace echolot_ld2460
}  // namespace esphome
