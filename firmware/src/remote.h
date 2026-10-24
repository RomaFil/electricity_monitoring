#pragma once
#include <Arduino.h>

// Command from the dashboard without opening any port: the server writes a line
//   svitlo-mode=monitor#<time of the press>   (or normal#...)
// into the description of the Healthchecks check, and the board reads it every 2 minutes
// with a READ-ONLY API key. A new value is applied once; switching on the ESP page also works.
//
// Signed commands go the same way: "svitlo-cmd=<command>#<time>#<HMAC-SHA256 with cmdKey>".
//   reboot        restart the board
//   sensor        look for the BME280 again (re-reads its calibration)
//   ota:<version> download firmware.bin.signed of release v<version> from GitHub and install it (ota.h)
// There is deliberately no remote factory reset: in the village the board would become an open
// SVITLO-LOCAL access point with nobody there to set it up. That stays the FLASH button (10 s).
void remoteLoop();

// Turns monitoring mode on/off and saves it (survives power cuts).
void setDisplayOn(bool on);

// Result of the last check, for the status page ("" = never ran).
extern String remoteStatus;
