#pragma once
#include <Arduino.h>

// Sends one heartbeat to cfg.url now. Updates st.last* fields. Returns true on HTTP 2xx.
bool heartbeatSend();
// Why the board started this time: "Power On", "Software/System restart", "Exception (29, epc1 0x4000df64)"...
String resetText();

// Call from loop(): sends a heartbeat when it is due and Wi-Fi is connected.
void heartbeatLoop();
