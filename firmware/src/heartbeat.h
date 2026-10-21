#pragma once

// Sends one heartbeat to cfg.url now. Updates st.last* fields. Returns true on HTTP 2xx.
bool heartbeatSend();

// Call from loop(): sends a heartbeat when it is due and Wi-Fi is connected.
void heartbeatLoop();
