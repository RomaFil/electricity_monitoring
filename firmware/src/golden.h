#pragma once
#include <Arduino.h>

// Golden copy: a known-good firmware kept as a file, and an automatic return to it when a new
// version does not prove itself. ESP8266 cannot run two images side by side like a Mars rover
// (the boot loader always starts the sketch at address 0), so the return is a normal update from
// that file: staged by Update, copied over the sketch by the boot loader.
//
// Why a file in LittleFS (1000 KB) and not raw free flash after the sketch: the /update page
// (ESP8266HTTPUpdateServer) reserves ALL free sketch space and stages right after the running
// sketch, so it overwrote a raw copy there (first test, 24.10.2026). Nothing but our own code
// writes LittleFS, and USB flashing of firmware.bin leaves it alone too.
//
// A version other than the golden one is "on trial" until it has sent a heartbeat (HTTP 200) and read
// the dashboard commands. If it does not manage that within 15 minutes, or it fails to start 3 times
// in a row, the board copies the golden image back and restarts. This code runs inside the new
// firmware, so it cannot help a version that dies before goldenBoot() or breaks goldenBoot() itself.

// First thing in setup() after the file system is mounted: counts starts of a version on trial
// and returns to the golden copy after too many.
void goldenBoot();
// In loop(): confirms the version on trial, or returns to the golden copy after the time limit.
// The very first confirmed run (no golden copy yet) saves itself as the golden copy.
void goldenLoop();
// Copies the running firmware to the golden area (only a confirmed version). Returns a result text.
String goldenSave();
// Version of the golden copy, "" = none.
String goldenVersion();
// true = this version has not proved itself yet.
bool goldenTrial();
// For the status page.
String goldenInfo();
