#pragma once

#include "esphome/components/number/number.h"
#include "esphome/components/select/select.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"
#include "esphome/components/uart/uart.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"

#include "ld2460_protocol.h"

namespace esphome {
namespace echolot_ld2460 {

using ::ld2460_protocol::LinkClock;
using ::ld2460_protocol::LinkState;
using ::ld2460_protocol::Mode;
using ::ld2460_protocol::Mounting;
using ::ld2460_protocol::Parser;
using ::ld2460_protocol::Report;

class EcholotLd2460;

// Installation mode ("side", "top"). Its state is what the module reads
// back; choosing an option asks the module, and the choice shows once the
// module confirms it.
class MountModeSelect : public select::Select, public Parented<EcholotLd2460> {
 protected:
  void control(size_t index) override;
};

// Mounting height (m) or tilt angle (°), with the same rule.
class MountNumber : public number::Number, public Parented<EcholotLd2460> {
 public:
  void set_is_angle(bool value) { this->is_angle_ = value; }

 protected:
  void control(float value) override;
  bool is_angle_{false};
};

class EcholotLd2460 : public Component, public uart::UARTDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  void set_stale_after(uint32_t ms) { this->stale_ms_ = ms; }
  void set_probe_interval(uint32_t ms) { this->probe_interval_ms_ = ms; }
  void set_frame_interval(uint32_t ms) { this->frame_interval_ms_ = ms; }
  void set_diagnostics_interval(uint32_t ms) { this->diagnostics_interval_ms_ = ms; }
  void set_quiet_means_empty(bool value) { this->quiet_means_empty_ = value; }

  void set_frame_text_sensor(text_sensor::TextSensor *s) { this->frame_sensor_ = s; }
  void set_status_text_sensor(text_sensor::TextSensor *s) { this->status_sensor_ = s; }
  void set_radar_firmware_text_sensor(text_sensor::TextSensor *s) { this->firmware_sensor_ = s; }
  void set_target_count_sensor(sensor::Sensor *s) { this->count_sensor_ = s; }
  void set_rx_bytes_sensor(sensor::Sensor *s) { this->rx_bytes_sensor_ = s; }
  void set_report_frames_sensor(sensor::Sensor *s) { this->reports_sensor_ = s; }
  void set_empty_frames_sensor(sensor::Sensor *s) { this->empty_sensor_ = s; }
  void set_rejected_frames_sensor(sensor::Sensor *s) { this->rejected_sensor_ = s; }
  void set_mount_mode_select(MountModeSelect *s) { this->mode_select_ = s; }
  void set_mount_height_number(MountNumber *n) { this->height_number_ = n; }
  void set_mount_angle_number(MountNumber *n) { this->angle_number_ = n; }

  // Ask the module to change its mounting. It is written once and read
  // back; the entities then show what the module holds.
  void request_mode(Mode mode);
  void request_height(float metres);
  void request_angle(float degrees);

 protected:
  void read_uart_(uint32_t now);
  void handle_ack_(uint32_t now);
  void maybe_probe_(uint32_t now, LinkState state);
  void publish_state_(LinkState state);
  void publish_frame_(uint32_t now, LinkState state);
  void publish_diagnostics_();
  void maybe_query_mounting_(uint32_t now);
  void request_mounting_(uint16_t height_cm, uint16_t angle_centideg);
  void read_back_mounting_();
  void publish_mounting_();
  void queue_command_(const uint8_t *bytes, size_t length);
  void send_queued_(uint32_t now);

  Parser parser_;
  Report latest_;
  LinkClock clock_;

  uint32_t stale_ms_{3000};
  uint32_t probe_interval_ms_{5000};
  uint32_t frame_interval_ms_{100};
  uint32_t diagnostics_interval_ms_{60000};
  bool quiet_means_empty_{false};

  uint32_t rx_bytes_{0};
  uint32_t reports_{0};
  uint32_t empty_reports_{0};

  uint32_t last_probe_ms_{0};
  bool probe_sent_{false};
  uint8_t version_queries_{0};
  uint32_t last_version_query_ms_{0};
  bool version_known_{false};

  uint32_t last_frame_publish_ms_{0};
  uint32_t published_seq_{0};
  bool frame_published_{false};
  uint32_t last_diagnostics_ms_{0};
  LinkState published_state_{LinkState::UNKNOWN};
  LinkState frame_state_{LinkState::UNKNOWN};
  bool state_published_{false};
  float published_count_{-1.0f};

  Mounting mounting_;
  // Height and angle go to the module in one command. What was asked for
  // and not yet read back is kept, so a height and an angle asked for one
  // after the other both arrive — the second command must not send the
  // old value of the first. -1: nothing pending.
  int32_t wanted_height_cm_{-1};
  int32_t wanted_angle_centideg_{-1};
  uint8_t mounting_queries_{0};
  uint32_t last_mounting_query_ms_{0};

  // Commands to the module go out one at a time, a little apart, so an
  // answer is not mixed up with the next command's.
  struct Command {
    uint8_t bytes[ld2460_protocol::SET_MOUNTING_LENGTH];
    uint8_t length;
  };
  static const uint8_t OUTBOX_SIZE = 6;
  Command outbox_[OUTBOX_SIZE];
  uint8_t outbox_used_{0};
  uint32_t last_command_ms_{0};

  text_sensor::TextSensor *frame_sensor_{nullptr};
  text_sensor::TextSensor *status_sensor_{nullptr};
  text_sensor::TextSensor *firmware_sensor_{nullptr};
  sensor::Sensor *count_sensor_{nullptr};
  sensor::Sensor *rx_bytes_sensor_{nullptr};
  sensor::Sensor *reports_sensor_{nullptr};
  sensor::Sensor *empty_sensor_{nullptr};
  sensor::Sensor *rejected_sensor_{nullptr};
  MountModeSelect *mode_select_{nullptr};
  MountNumber *height_number_{nullptr};
  MountNumber *angle_number_{nullptr};
};

}  // namespace echolot_ld2460
}  // namespace esphome
