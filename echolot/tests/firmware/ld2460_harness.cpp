// Drives ld2460_protocol.h from a line protocol on stdin, so the tests in
// tests/test_ld2460_protocol.py exercise the exact code the device runs.
//
//   feed <hex>        bytes into the current parser; one output line per
//                     event ("report <n> <x>,<y>;..." or "ack <fn> <hex>"),
//                     then "end rejected=<n>". Parser state carries over
//                     between feed lines, which is how split frames are
//                     tested.
//   reset             a fresh parser
//   encode <S> <seq> <targets|->
//   classify <have_report> <last_report> <have_ack> <last_ack> <enabled>
//            <now> <stale> <ack_valid>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>

#include "../../app/esphome_components/echolot_ld2460/ld2460_protocol.h"

using namespace ld2460_protocol;

static int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

int main() {
  Parser *parser = new Parser();
  std::string line;
  while (std::getline(std::cin, line)) {
    std::istringstream in(line);
    std::string command;
    in >> command;
    if (command == "reset") {
      delete parser;
      parser = new Parser();
      std::cout << "ok\n";
    } else if (command == "feed") {
      std::string hex;
      in >> hex;
      for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        const uint8_t byte = uint8_t(hex_value(hex[i]) * 16 + hex_value(hex[i + 1]));
        switch (parser->feed(byte)) {
          case Event::REPORT: {
            const Report &r = parser->report();
            std::cout << "report " << int(r.count) << " ";
            for (uint8_t t = 0; t < r.count; ++t)
              std::cout << (t ? ";" : "") << r.targets[t].x_dm << "," << r.targets[t].y_dm;
            std::cout << "\n";
            break;
          }
          case Event::ACK: {
            const Ack &a = parser->ack();
            char fn[3];
            std::snprintf(fn, sizeof(fn), "%02x", a.function);
            std::cout << "ack " << fn << " ";
            for (uint8_t p = 0; p < a.payload_length; ++p) {
              char b[3];
              std::snprintf(b, sizeof(b), "%02x", a.payload[p]);
              std::cout << b;
            }
            std::cout << "\n";
            break;
          }
          case Event::NONE:
            break;
        }
      }
      std::cout << "end rejected=" << parser->rejected << "\n";
    } else if (command == "encode") {
      std::string state, targets;
      unsigned long seq = 0;
      in >> state >> seq >> targets;
      LinkState s = state == "R" ? LinkState::RECEIVING : state == "Q" ? LinkState::QUIET : LinkState::UNKNOWN;
      Report report;
      if (targets != "-") {
        std::istringstream list(targets);
        std::string pair;
        while (std::getline(list, pair, ';') && report.count < MAX_TARGETS) {
          const size_t comma = pair.find(',');
          report.targets[report.count].x_dm = int16_t(std::atoi(pair.substr(0, comma).c_str()));
          report.targets[report.count].y_dm = int16_t(std::atoi(pair.substr(comma + 1).c_str()));
          report.count++;
        }
      }
      char text[FRAME_TEXT_CAPACITY];
      const size_t n = encode_frame(text, sizeof(text), s, uint32_t(seq), report);
      std::cout << (n ? text : "<empty>") << "\n";
    } else if (command == "classify") {
      LinkClock clock;
      unsigned long have_report, last_report, have_ack, last_ack, enabled, now, stale, ack_valid;
      in >> have_report >> last_report >> have_ack >> last_ack >> enabled >> now >> stale >> ack_valid;
      clock.have_report = have_report;
      clock.last_report_ms = uint32_t(last_report);
      clock.have_ack = have_ack;
      clock.last_ack_ms = uint32_t(last_ack);
      clock.reporting_enabled = enabled;
      std::cout << state_letter(classify(clock, uint32_t(now), uint32_t(stale), uint32_t(ack_valid))) << "\n";
    }
    std::cout.flush();
  }
  delete parser;
  return 0;
}
