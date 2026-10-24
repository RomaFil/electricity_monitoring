#pragma once
#include <Arduino.h>
#include <ESP8266WiFiMulti.h>

// Firmware update from far away, without opening any port: the dashboard sends the signed
// command "ota:<version>" (remote.cpp), the board restarts and right at boot downloads
//   https://github.com/RomaFil/electricity_monitoring/releases/download/v<version>/firmware.bin.signed
// The image carries an RSA-2048 signature made with private.key, which exists only on Roman's PC.
// The board checks it against public.key (built in) before it switches to the new image, so neither
// GitHub, nor the network, nor a stolen VPS can make it run a foreign firmware.
// With the signature on, the /update page also accepts only signed images.

// Turns the signature check on if the firmware was built with firmware/public.key. Call first in setup().
void otaSignatureBegin();
// true = only signed images are accepted (and remote updates are allowed at all).
bool otaSigned();

// "0.7.1": digits and dots only, as in the release tag v0.7.1.
bool otaVersionOk(const String &v);
// Remembers the version to download at the next boot. The caller restarts the board.
bool otaRequest(const String &version);
// In setup(), after the Wi-Fi networks are added: finishes the report of the previous attempt
// and, if an update was requested, downloads and installs it (on success the board restarts).
void otaBoot(ESP8266WiFiMulti &multi);

// Replaces the result text (the return to the golden copy reports through it).
void otaNote(const String &text);

// Result of the last update attempt for the status page and the ping body ("" = never).
extern String otaStatus;
