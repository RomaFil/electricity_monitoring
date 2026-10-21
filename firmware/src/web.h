#pragma once

// Web pages: status (/), settings (/settings), factory reset (/reset), OTA (/update), JSON (/api/status).
void webBegin();
void webLoop();
// Captive portal DNS: every name resolves to the board while the setup AP is on.
void webCaptive(bool on);
