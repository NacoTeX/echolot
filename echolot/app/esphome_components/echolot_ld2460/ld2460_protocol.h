#pragma once
// HLK-LD2460 serial protocol, and the one line Echolot reads from it.
//
// Deliberately free of ESPHome: everything in here is plain C++ so the
// same code the device runs can be compiled and exercised on a PC
// (tests/firmware/ld2460_harness.cpp). The component around it only moves
// bytes in and strings out.
//
// Where the facts come from:
//   * Report frames (radar -> host): Hi-Link "HLK-LD2460 Serial port
//     communication protocol V1.0", table 2, including its worked example
//     F4 F3 F2 F1 04 0F 00 0F 00 17 00 F8 F7 F6 F5 for a target at
//     (1.5 m, 2.3 m).
//   * Command frames and their acknowledgements: same document, table 3/4,
//     for 0x06 "open/close reporting". The layout of the 0x06 and 0x0B
//     acknowledgement payloads is taken from smarthomeshop/ld2460 (MIT,
//     itself based on ciriousjoker/esphome_ld2460), because the part of
//     the manual that describes them was not available to us. Treated as
//     unverified until a real module answers.
//   * Installation mode and parameters (0x07-0x0A), from the same
//     smarthomeshop/ld2460 code and its LD2460-UPGRADE guide, which cite
//     Hi-Link's manual: the module is mounted "side" (on a wall) or "top"
//     (on the ceiling), and keeps a mounting height and tilt angle across
//     power cycles. The module uses them itself; what exactly it does with
//     them is not documented to us. Unverified until a real module answers
//     — which is why the firmware only ever publishes what the module
//     reads back, never what was asked for.
//   * The parser itself is the one from the wohnzimmer-radar prototype
//     (firmware 0.2.1), which received real frames from a module on
//     2026-09-23; extended here by the second frame family.
//
// What is not known and therefore not assumed: whether the module keeps
// sending empty reports in an empty room, or falls silent. See
// classify() — the answer decides what silence means, and the firmware
// reports the difference instead of guessing it.

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace ld2460_protocol {

static const uint8_t MAX_TARGETS = 5;
// 11 bytes of framing plus 4 per target; five targets is the most the
// manual promises and the most the length field is allowed to announce.
static const size_t MAX_REPORT_LENGTH = 11 + 4 * MAX_TARGETS;
// No acknowledgement we know of carries more than five payload bytes
// (0x0B, firmware version). Sixteen leaves room without letting a
// corrupted length field hold the parser hostage.
static const size_t MAX_ACK_PAYLOAD = 16;
static const size_t MAX_ACK_LENGTH = 11 + MAX_ACK_PAYLOAD;

static const uint8_t FUNCTION_REPORT = 0x04;
static const uint8_t FUNCTION_REPORTING = 0x06;
static const uint8_t FUNCTION_SET_MOUNTING = 0x07;
static const uint8_t FUNCTION_QUERY_MOUNTING = 0x08;
static const uint8_t FUNCTION_SET_MODE = 0x09;
static const uint8_t FUNCTION_QUERY_MODE = 0x0A;
static const uint8_t FUNCTION_VERSION = 0x0B;

// "Open reporting". Reporting is on by default; sending this changes
// nothing on a module in its normal state, and the acknowledgement it
// earns is the only way to tell a quiet module from an absent one.
static const uint8_t COMMAND_ENABLE_REPORTING[12] = {0xFD, 0xFC, 0xFB, 0xFA, FUNCTION_REPORTING, 0x0C, 0x00,
                                                     0x01, 0x04, 0x03, 0x02, 0x01};
// Read-only: asks for the module's firmware version.
static const uint8_t COMMAND_QUERY_VERSION[12] = {0xFD, 0xFC, 0xFB, 0xFA, FUNCTION_VERSION, 0x0C, 0x00,
                                                  0x01, 0x04, 0x03, 0x02, 0x01};
// Read-only: the installation mode the module holds.
static const uint8_t COMMAND_QUERY_MODE[12] = {0xFD, 0xFC, 0xFB, 0xFA, FUNCTION_QUERY_MODE, 0x0C, 0x00,
                                               0x01, 0x04, 0x03, 0x02, 0x01};
// Read-only: the mounting height and angle the module holds.
static const uint8_t COMMAND_QUERY_MOUNTING[12] = {0xFD, 0xFC, 0xFB, 0xFA, FUNCTION_QUERY_MOUNTING, 0x0C, 0x00,
                                                   0x01, 0x04, 0x03, 0x02, 0x01};

// How the module is mounted, as it numbers it.
enum class Mode : uint8_t { UNKNOWN = 0, SIDE = 1, TOP = 2 };

// What the firmware will write. Hi-Link recommends 2.2-2.7 m and 25-40°
// for a wall; the limits are wider so a room that needs something else
// can have it, and narrow enough that a typo does not reach the module.
static const uint16_t MIN_HEIGHT_CM = 50;
static const uint16_t MAX_HEIGHT_CM = 500;
static const uint16_t MAX_ANGLE_CENTIDEG = 9000;
static const size_t SET_MODE_LENGTH = 12;
static const size_t SET_MOUNTING_LENGTH = 15;

struct Target {
  int16_t x_dm;  // decimetres, as reported; sign convention unverified
  int16_t y_dm;
};

struct Report {
  uint8_t count = 0;
  Target targets[MAX_TARGETS] = {};
};

struct Ack {
  uint8_t function = 0;
  uint8_t payload_length = 0;
  uint8_t payload[MAX_ACK_PAYLOAD] = {};
};

enum class Event : uint8_t { NONE, REPORT, ACK };

class Parser {
 public:
  // Frames that started with a valid header and then turned out wrong:
  // bad function, bad length, bad tail. Stray bytes between frames are
  // skipped silently, since a partial frame at boot is normal.
  uint32_t rejected = 0;

  void reset() { this->used_ = 0; }

  Event feed(uint8_t byte) {
    if (this->used_ == sizeof(this->buffer_))
      this->discard_();
    this->buffer_[this->used_++] = byte;
    while (this->used_ > 0) {
      const size_t prefix = this->used_ < 4 ? this->used_ : 4;
      const bool report = memcmp(this->buffer_, REPORT_HEADER, prefix) == 0;
      const bool ack = !report && memcmp(this->buffer_, ACK_HEADER, prefix) == 0;
      if (!report && !ack) {
        this->discard_();
        continue;
      }
      if (this->used_ < 7)
        return Event::NONE;
      const size_t length = size_t(this->buffer_[5]) | (size_t(this->buffer_[6]) << 8);
      if (report) {
        if (this->buffer_[4] != FUNCTION_REPORT || length < 11 || length > MAX_REPORT_LENGTH || (length - 11) % 4) {
          ++this->rejected;
          this->discard_();
          continue;
        }
      } else if (length < 11 || length > MAX_ACK_LENGTH) {
        ++this->rejected;
        this->discard_();
        continue;
      }
      if (this->used_ < length)
        return Event::NONE;
      const uint8_t *tail = report ? REPORT_TAIL : ACK_TAIL;
      if (memcmp(this->buffer_ + length - 4, tail, 4) != 0) {
        ++this->rejected;
        this->discard_();
        continue;
      }
      Event event;
      if (report) {
        this->report_ = Report{};
        this->report_.count = uint8_t((length - 11) / 4);
        for (uint8_t i = 0; i < this->report_.count; ++i) {
          this->report_.targets[i] = {signed_le_(this->buffer_ + 7 + i * 4), signed_le_(this->buffer_ + 9 + i * 4)};
        }
        event = Event::REPORT;
      } else {
        this->ack_ = Ack{};
        this->ack_.function = this->buffer_[4];
        this->ack_.payload_length = uint8_t(length - 11);
        memcpy(this->ack_.payload, this->buffer_ + 7, this->ack_.payload_length);
        event = Event::ACK;
      }
      this->used_ -= length;
      memmove(this->buffer_, this->buffer_ + length, this->used_);
      return event;
    }
    return Event::NONE;
  }

  const Report &report() const { return this->report_; }
  const Ack &ack() const { return this->ack_; }

 private:
  static int16_t signed_le_(const uint8_t *p) {
    const uint16_t v = uint16_t(p[0]) | uint16_t(uint16_t(p[1]) << 8);
    return v >= 0x8000 ? int16_t(int32_t(v) - 65536) : int16_t(v);
  }
  void discard_() {
    --this->used_;
    memmove(this->buffer_, this->buffer_ + 1, this->used_);
  }

  static constexpr uint8_t REPORT_HEADER[4] = {0xF4, 0xF3, 0xF2, 0xF1};
  static constexpr uint8_t REPORT_TAIL[4] = {0xF8, 0xF7, 0xF6, 0xF5};
  static constexpr uint8_t ACK_HEADER[4] = {0xFD, 0xFC, 0xFB, 0xFA};
  static constexpr uint8_t ACK_TAIL[4] = {0x04, 0x03, 0x02, 0x01};

  size_t used_ = 0;
  uint8_t buffer_[64] = {};
  Report report_;
  Ack ack_;
};

// The command that sets the installation mode; 0 bytes for a mode the
// module does not have.
inline size_t encode_set_mode(uint8_t *out, size_t capacity, Mode mode) {
  if (capacity < SET_MODE_LENGTH || (mode != Mode::SIDE && mode != Mode::TOP))
    return 0;
  const uint8_t command[SET_MODE_LENGTH] = {0xFD, 0xFC, 0xFB, 0xFA, FUNCTION_SET_MODE, 0x0C, 0x00,
                                            uint8_t(mode), 0x04, 0x03, 0x02, 0x01};
  memcpy(out, command, SET_MODE_LENGTH);
  return SET_MODE_LENGTH;
}

// The command that sets height (centimetres) and angle (hundredths of a
// degree), both little-endian; 0 bytes outside the limits above.
inline size_t encode_set_mounting(uint8_t *out, size_t capacity, uint16_t height_cm, uint16_t angle_centideg) {
  if (capacity < SET_MOUNTING_LENGTH || height_cm < MIN_HEIGHT_CM || height_cm > MAX_HEIGHT_CM ||
      angle_centideg > MAX_ANGLE_CENTIDEG)
    return 0;
  const uint8_t command[SET_MOUNTING_LENGTH] = {
      0xFD, 0xFC, 0xFB, 0xFA, FUNCTION_SET_MOUNTING, 0x0F, 0x00,
      uint8_t(height_cm & 0xFF), uint8_t(height_cm >> 8), uint8_t(angle_centideg & 0xFF), uint8_t(angle_centideg >> 8),
      0x04, 0x03, 0x02, 0x01};
  memcpy(out, command, SET_MOUNTING_LENGTH);
  return SET_MOUNTING_LENGTH;
}

// What the module has said about its mounting. Only its answers go in.
struct Mounting {
  Mode mode = Mode::UNKNOWN;
  bool params_known = false;
  uint16_t height_cm = 0;
  uint16_t angle_centideg = 0;
};

enum class MountingEvent : uint8_t { NONE, MODE, PARAMS, SET_OK, SET_FAILED };

// Take one acknowledgement into `mounting`. A set is acknowledged, but
// what the module holds afterwards is only what it reads back: SET_OK and
// SET_FAILED change nothing here, and the caller asks again.
inline MountingEvent read_mounting_ack(Mounting &mounting, const Ack &ack) {
  const uint8_t *p = ack.payload;
  switch (ack.function) {
    case FUNCTION_QUERY_MODE:
    case FUNCTION_VERSION:
      // The version answer starts with the mode as well.
      if (ack.payload_length >= (ack.function == FUNCTION_VERSION ? 5 : 1) && (p[0] == 1 || p[0] == 2)) {
        mounting.mode = Mode(p[0]);
        return MountingEvent::MODE;
      }
      return MountingEvent::NONE;
    case FUNCTION_QUERY_MOUNTING:
      if (ack.payload_length < 4)
        return MountingEvent::NONE;
      mounting.params_known = true;
      mounting.height_cm = uint16_t(p[0] | (p[1] << 8));
      mounting.angle_centideg = uint16_t(p[2] | (p[3] << 8));
      return MountingEvent::PARAMS;
    case FUNCTION_SET_MODE:
      if (ack.payload_length < 1)
        return MountingEvent::NONE;
      return (p[0] & 0x10) ? MountingEvent::SET_OK : MountingEvent::SET_FAILED;
    case FUNCTION_SET_MOUNTING:
      if (ack.payload_length < 1)
        return MountingEvent::NONE;
      return p[0] == 0x01 ? MountingEvent::SET_OK : MountingEvent::SET_FAILED;
    default:
      return MountingEvent::NONE;
  }
}

inline const char *mode_name(Mode mode) {
  switch (mode) {
    case Mode::SIDE:
      return "side";
    case Mode::TOP:
      return "top";
    default:
      return "unknown";
  }
}

// What the link to the module is doing right now.
//
//   RECEIVING  a report arrived within `stale_ms`. Its targets are current.
//   QUIET      no report, but the module acknowledged "open reporting"
//              recently and said reporting is on. It is there and has
//              nothing to say.
//   UNKNOWN    neither. Wiring, power, a module still booting — anything.
//
// QUIET is the case the manual leaves open. If the module sends empty
// reports in an empty room, QUIET never happens outside a fault; if it
// falls silent, QUIET is what an empty room looks like. Which one is true
// is a fact about the hardware, and the component makes it a setting
// (quiet_means_empty) rather than an assumption.
enum class LinkState : uint8_t { UNKNOWN, QUIET, RECEIVING };

struct LinkClock {
  bool have_report = false;
  uint32_t last_report_ms = 0;
  bool have_ack = false;
  uint32_t last_ack_ms = 0;
  bool reporting_enabled = false;
};

// Pure, so the rule can be tested without a clock. Arithmetic is on
// unsigned 32-bit milliseconds, which is what millis() wraps at.
inline LinkState classify(const LinkClock &clock, uint32_t now_ms, uint32_t stale_ms, uint32_t ack_valid_ms) {
  if (clock.have_report && uint32_t(now_ms - clock.last_report_ms) < stale_ms)
    return LinkState::RECEIVING;
  if (clock.have_ack && clock.reporting_enabled && uint32_t(now_ms - clock.last_ack_ms) < ack_valid_ms)
    return LinkState::QUIET;
  return LinkState::UNKNOWN;
}

inline char state_letter(LinkState state) {
  switch (state) {
    case LinkState::RECEIVING:
      return 'R';
    case LinkState::QUIET:
      return 'Q';
    default:
      return 'U';
  }
}

// The line Echolot reads, format version 1:
//
//     1|<state>|<seq>|<targets>
//
// state    R, Q or U as above.
// seq      reports received since boot. A new value means a new report; a
//          smaller one means the device restarted.
// targets  empty, or "x,y" pairs in decimetres joined by ";". Only ever
//          filled for R: coordinates of a report that is no longer
//          current are not a measurement.
//
// One string per report, so X and Y of one target always come from the
// same frame — which separate per-coordinate entities cannot promise.
inline size_t encode_frame(char *out, size_t capacity, LinkState state, uint32_t seq, const Report &report) {
  if (capacity == 0)
    return 0;
  int written = snprintf(out, capacity, "1|%c|%lu|", state_letter(state), (unsigned long) seq);
  if (written < 0 || size_t(written) >= capacity) {
    out[0] = '\0';
    return 0;
  }
  size_t used = size_t(written);
  if (state == LinkState::RECEIVING) {
    for (uint8_t i = 0; i < report.count && i < MAX_TARGETS; ++i) {
      written = snprintf(out + used, capacity - used, "%s%d,%d", i ? ";" : "", int(report.targets[i].x_dm),
                         int(report.targets[i].y_dm));
      if (written < 0 || size_t(written) >= capacity - used) {
        out[0] = '\0';
        return 0;
      }
      used += size_t(written);
    }
  }
  return used;
}

// "1|U|4294967295|" plus five "-32768,-32768" and four separators.
static const size_t FRAME_TEXT_CAPACITY = 16 + MAX_TARGETS * 13 + (MAX_TARGETS - 1) + 1;

}  // namespace ld2460_protocol
