#pragma once
#include <Arduino.h>

// Command from the dashboard without opening any port: the server writes a line
//   svitlo-mode=monitor#<time of the press>   (or normal#...)
// into the description of the Healthchecks check, and the board reads it every 2 minutes
// with a READ-ONLY API key. A new value is applied once; switching on the ESP page also works.
void remoteLoop();

// Turns monitoring mode on/off and saves it (survives power cuts).
void setDisplayOn(bool on);

// Result of the last check, for the status page ("" = never ran).
extern String remoteStatus;
