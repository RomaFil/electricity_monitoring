#pragma once
#include <Arduino.h>

#include "config.h"

// Runtime state shared by the modules (no settings here, those are in Config).
struct State {
  Config cfg;
  uint32_t bootCount = 0;
  bool apActive = false;
  String wifiSeen;  // access points of the configured networks in the last scan: "ssid bssid ch N -60 dBm; ..."

  // heartbeat
  uint32_t lastAttemptMs = 0;   // millis() of the last attempt (0 = never)
  uint32_t lastOkMs = 0;        // millis() of the last success (0 = never)
  int lastCode = 0;             // HTTP code, or negative HTTPClient error
  String lastError;
  uint32_t failsInRow = 0;
  uint32_t failsTotal = 0;
  uint32_t sentTotal = 0;
};

extern State st;

// Uptime in seconds as a 64-bit value (millis() wraps after 49 days, this does not).
uint64_t uptimeSeconds();
bool timeValid();
// "AA:BB:CC:DD:EE:FF" -> 6 bytes; false if it is not a MAC address.
bool parseBssid(const String &s, uint8_t out[6]);
String fmtAgo(uint32_t ms);
