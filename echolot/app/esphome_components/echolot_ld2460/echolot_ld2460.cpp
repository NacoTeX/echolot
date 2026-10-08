#include "echolot_ld2460.h"

#include <cmath>
#include <cstring>

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
// The version is asked for two seconds after boot, three times five
// seconds apart, then once a minute until the module answers
// (proto::query_due). Before 2.0 it was asked three times and then never
// again: a module that came up late stayed without a version until the
// ESP restarted.
static const proto::QuerySchedule VERSION_QUERIES{2000, 3, 5000, 60000};
// Even with nothing new to say, the frame line is refreshed this often,
// so Echolot can tell a silent device from a lost connection.
static const uint32_t FRAME_HEARTBEAT_MS = 1000;
static const uint32_t FIRST_DIAGNOSTICS_MS = 10000;
// Mounting and range are asked for after the version, on the same
// schedule, for as long as the module has not answered.
static const proto::QuerySchedule MOUNTING_QUERIES{3000, 3, 5000, 60000};
static const uint32_t COMMAND_GAP_MS = 200;

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
  // Nothing is written to the module on boot. Reporting is on from the
  // factory; the mounting is read, and changed only when asked to.
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

  if (!this->version_known_ &&
      proto::query_due(VERSION_QUERIES, this->version_queries_, this->last_version_query_ms_, now)) {
    this->write_array(proto::COMMAND_QUERY_VERSION, sizeof(proto::COMMAND_QUERY_VERSION));
    if (this->version_queries_ < 255)
      this->version_queries_++;
    this->last_version_query_ms_ = now;
  }

  this->maybe_query_mounting_(now);
  this->send_queued_(now);

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
  switch (proto::read_range_ack(this->range_, ack)) {
    case proto::RangeEvent::PARAMS: {
      const int32_t held[RANGE_PARTS] = {this->range_.distance_dm, this->range_.start_decideg,
                                         this->range_.end_decideg};
      for (uint8_t i = 0; i < RANGE_PARTS; ++i) {
        if (this->range_pending_[i] && this->range_wanted_[i] == held[i])
          this->range_pending_[i] = false;
      }
      ESP_LOGI(TAG, "Module detection range: %u dm, %d to %d decidegrees", this->range_.distance_dm,
               this->range_.start_decideg, this->range_.end_decideg);
      this->publish_range_();
      return;
    }
    case proto::RangeEvent::SET_OK:
      ESP_LOGI(TAG, "Module accepted the detection range");
      this->read_back_range_();
      return;
    case proto::RangeEvent::SET_FAILED:
      ESP_LOGW(TAG, "Module refused the detection range");
      for (bool &pending : this->range_pending_)
        pending = false;
      this->read_back_range_();
      this->publish_range_();
      return;
    default:
      break;
  }
  const Mode mode_before = this->mounting_.mode;
  switch (proto::read_mounting_ack(this->mounting_, ack)) {
    case proto::MountingEvent::PARAMS:
      if (this->wanted_height_cm_ == this->mounting_.height_cm)
        this->wanted_height_cm_ = -1;
      if (this->wanted_angle_centideg_ == this->mounting_.angle_centideg)
        this->wanted_angle_centideg_ = -1;
      // fall through
    case proto::MountingEvent::MODE:
      ESP_LOGI(TAG, "Module mounting: %s, %u cm, %u.%02u deg", proto::mode_name(this->mounting_.mode),
               this->mounting_.height_cm, this->mounting_.angle_centideg / 100, this->mounting_.angle_centideg % 100);
      this->publish_mounting_();
      if (mode_before != Mode::UNKNOWN && this->mounting_.mode != mode_before) {
        // Another mode, another detection range (read_back_mounting_):
        // what was held or asked for belonged to the old one.
        this->range_ = Range{};
        for (bool &pending : this->range_pending_)
          pending = false;
        this->read_back_range_();
      }
      break;
    case proto::MountingEvent::SET_OK:
      ESP_LOGI(TAG, "Module accepted the mounting change (function 0x%02X)", ack.function);
      this->read_back_mounting_();
      return;
    case proto::MountingEvent::SET_FAILED:
      ESP_LOGW(TAG, "Module refused the mounting change (function 0x%02X)", ack.function);
      this->wanted_height_cm_ = this->wanted_angle_centideg_ = -1;
      this->read_back_mounting_();
      // Back to what the module holds, so nobody is shown the refused value.
      this->publish_mounting_();
      return;
    case proto::MountingEvent::NONE:
      break;
  }
  if (ack.function == proto::FUNCTION_QUERY_MODE || ack.function == proto::FUNCTION_QUERY_MOUNTING)
    return;
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

void EcholotLd2460::maybe_query_mounting_(uint32_t now) {
  const bool mode_known = this->mounting_.mode != Mode::UNKNOWN;
  if (mode_known && this->mounting_.params_known && this->range_.known)
    return;
  if (!proto::query_due(MOUNTING_QUERIES, this->mounting_queries_, this->last_mounting_query_ms_, now))
    return;
  if (!mode_known)
    this->queue_command_(proto::COMMAND_QUERY_MODE, sizeof(proto::COMMAND_QUERY_MODE));
  if (!this->mounting_.params_known)
    this->queue_command_(proto::COMMAND_QUERY_MOUNTING, sizeof(proto::COMMAND_QUERY_MOUNTING));
  if (!this->range_.known)
    this->queue_command_(proto::COMMAND_QUERY_RANGE, sizeof(proto::COMMAND_QUERY_RANGE));
  if (this->mounting_queries_ < 255)
    this->mounting_queries_++;
  this->last_mounting_query_ms_ = now;
}

void EcholotLd2460::read_back_mounting_() {
  this->queue_command_(proto::COMMAND_QUERY_MODE, sizeof(proto::COMMAND_QUERY_MODE));
  this->queue_command_(proto::COMMAND_QUERY_MOUNTING, sizeof(proto::COMMAND_QUERY_MOUNTING));
  // The module keeps one detection range per mode and answers with the
  // current mode's; after a change of mode it is another one.
  this->read_back_range_();
}

void EcholotLd2460::read_back_range_() {
  this->queue_command_(proto::COMMAND_QUERY_RANGE, sizeof(proto::COMMAND_QUERY_RANGE));
}

void EcholotLd2460::request_range(uint8_t part, float value) {
  if (part >= RANGE_PARTS)
    return;
  if (!this->range_.known || this->mounting_.mode == Mode::UNKNOWN) {
    // The three parts go to the module together, and its limits depend
    // on the mode: without both read back, nothing honest can be sent.
    ESP_LOGW(TAG, "Detection range or mode not read from the module yet; asking it first");
    this->read_back_mounting_();
    this->publish_range_();
    return;
  }
  const long wanted = lroundf(value * 10.0f);
  const bool fits = part == RANGE_DISTANCE ? (wanted >= 0 && wanted <= 255) : (wanted >= -32768 && wanted <= 32767);
  const int32_t held[RANGE_PARTS] = {this->range_.distance_dm, this->range_.start_decideg, this->range_.end_decideg};
  int32_t send[RANGE_PARTS];
  for (uint8_t i = 0; i < RANGE_PARTS; ++i)
    send[i] = this->range_pending_[i] ? this->range_wanted_[i] : held[i];
  send[part] = int32_t(wanted);
  uint8_t command[proto::MAX_COMMAND_LENGTH];
  const size_t length =
      fits ? proto::encode_set_range(command, sizeof(command), this->mounting_.mode, uint8_t(send[RANGE_DISTANCE]),
                                     int16_t(send[RANGE_START]), int16_t(send[RANGE_END]))
           : 0;
  if (length == 0) {
    ESP_LOGW(TAG, "Detection range %ld dm / %ld to %ld decidegrees is outside what this firmware writes in %s mode",
             long(send[RANGE_DISTANCE]), long(send[RANGE_START]), long(send[RANGE_END]),
             proto::mode_name(this->mounting_.mode));
    // Back to what the module holds, so nobody is shown the refused value.
    this->publish_range_();
    return;
  }
  this->range_wanted_[part] = send[part];
  this->range_pending_[part] = true;
  ESP_LOGI(TAG, "Asking the module for %ld dm, %ld to %ld decidegrees", long(send[RANGE_DISTANCE]),
           long(send[RANGE_START]), long(send[RANGE_END]));
  this->queue_command_(command, length);
}

void EcholotLd2460::publish_range_() {
  if (!this->range_.known)
    return;
  const float values[RANGE_PARTS] = {this->range_.distance_dm / 10.0f, this->range_.start_decideg / 10.0f,
                                     this->range_.end_decideg / 10.0f};
  for (uint8_t i = 0; i < RANGE_PARTS; ++i) {
    if (this->range_numbers_[i] != nullptr)
      this->range_numbers_[i]->publish_state(values[i]);
  }
}

void EcholotLd2460::request_mode(Mode mode) {
  uint8_t command[proto::SET_MOUNTING_LENGTH];
  const size_t length = proto::encode_set_mode(command, sizeof(command), mode);
  if (length == 0) {
    ESP_LOGW(TAG, "Not a mounting mode the module has");
    this->publish_mounting_();
    return;
  }
  ESP_LOGI(TAG, "Asking the module for mounting mode %s", proto::mode_name(mode));
  this->queue_command_(command, length);
}

void EcholotLd2460::request_height(float metres) {
  if (!this->mounting_.params_known) {
    // Height and angle go to the module together; without the angle it
    // holds, there is nothing honest to send with the new height.
    ESP_LOGW(TAG, "Mounting not read from the module yet; asking it first");
    this->read_back_mounting_();
    this->publish_mounting_();
    return;
  }
  this->wanted_height_cm_ = lroundf(metres * 100.0f);
  this->request_mounting_(uint16_t(this->wanted_height_cm_),
                          this->wanted_angle_centideg_ >= 0 ? uint16_t(this->wanted_angle_centideg_)
                                                            : this->mounting_.angle_centideg);
}

void EcholotLd2460::request_angle(float degrees) {
  if (!this->mounting_.params_known) {
    ESP_LOGW(TAG, "Mounting not read from the module yet; asking it first");
    this->read_back_mounting_();
    this->publish_mounting_();
    return;
  }
  this->wanted_angle_centideg_ = lroundf(degrees * 100.0f);
  this->request_mounting_(this->wanted_height_cm_ >= 0 ? uint16_t(this->wanted_height_cm_) : this->mounting_.height_cm,
                          uint16_t(this->wanted_angle_centideg_));
}

void EcholotLd2460::request_mounting_(uint16_t height_cm, uint16_t angle_centideg) {
  uint8_t command[proto::SET_MOUNTING_LENGTH];
  const size_t length = proto::encode_set_mounting(command, sizeof(command), height_cm, angle_centideg);
  if (length == 0) {
    ESP_LOGW(TAG, "Mounting %u cm / %u cdeg is outside what this firmware writes", height_cm, angle_centideg);
    this->wanted_height_cm_ = this->wanted_angle_centideg_ = -1;
    this->publish_mounting_();
    return;
  }
  ESP_LOGI(TAG, "Asking the module for %u cm, %u.%02u deg", height_cm, angle_centideg / 100, angle_centideg % 100);
  this->queue_command_(command, length);
}

void EcholotLd2460::publish_mounting_() {
  const Mounting &m = this->mounting_;
  if (this->mode_select_ != nullptr && m.mode != Mode::UNKNOWN)
    this->mode_select_->publish_state(size_t(m.mode == Mode::SIDE ? 0 : 1));
  if (!m.params_known)
    return;
  if (this->height_number_ != nullptr)
    this->height_number_->publish_state(m.height_cm / 100.0f);
  if (this->angle_number_ != nullptr)
    this->angle_number_->publish_state(m.angle_centideg / 100.0f);
}

void EcholotLd2460::queue_command_(const uint8_t *bytes, size_t length) {
  if (length > sizeof(Command::bytes))
    return;
  for (uint8_t i = 0; i < this->outbox_used_; ++i) {
    // The same question twice in a row is answered once.
    if (this->outbox_[i].length == length && memcmp(this->outbox_[i].bytes, bytes, length) == 0)
      return;
  }
  if (this->outbox_used_ == OUTBOX_SIZE) {
    ESP_LOGW(TAG, "Too many commands for the module at once; dropping one");
    return;
  }
  Command &slot = this->outbox_[this->outbox_used_++];
  memcpy(slot.bytes, bytes, length);
  slot.length = uint8_t(length);
}

void EcholotLd2460::send_queued_(uint32_t now) {
  if (this->outbox_used_ == 0 || now - this->last_command_ms_ < COMMAND_GAP_MS)
    return;
  this->write_array(this->outbox_[0].bytes, this->outbox_[0].length);
  this->outbox_used_--;
  memmove(this->outbox_, this->outbox_ + 1, this->outbox_used_ * sizeof(Command));
  this->last_command_ms_ = now;
}

void MountModeSelect::control(size_t index) {
  this->parent_->request_mode(index == 0 ? Mode::SIDE : Mode::TOP);
}

void RangeNumber::control(float value) { this->parent_->request_range(this->part_, value); }

void MountNumber::control(float value) {
  if (this->is_angle_)
    this->parent_->request_angle(value);
  else
    this->parent_->request_height(value);
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
  LOG_SELECT("  ", "Mount mode", this->mode_select_);
  LOG_NUMBER("  ", "Mount height", this->height_number_);
  LOG_NUMBER("  ", "Mount angle", this->angle_number_);
  LOG_NUMBER("  ", "Range distance", this->range_numbers_[RANGE_DISTANCE]);
  LOG_NUMBER("  ", "Range start", this->range_numbers_[RANGE_START]);
  LOG_NUMBER("  ", "Range end", this->range_numbers_[RANGE_END]);
}

}  // namespace echolot_ld2460
}  // namespace esphome
