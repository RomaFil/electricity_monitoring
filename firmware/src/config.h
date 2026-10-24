#pragma once
#include <Arduino.h>

// Everything the user sets on the web page. Stored in LittleFS as /config.json.
struct WifiNet {
  String ssid;
  String pass;
};

struct Config {
  WifiNet nets[3];          // main network + 2 backups, tried in this order
  String url;               // https://hc-ping.com/<uuid>  or  http://<server>/api/heartbeat
  String deviceKey;         // sent as X-Device-Key (direct mode); empty = not sent
  uint32_t intervalS = 60;  // seconds between heartbeats
  String tlsMode = "ca";    // ca | fingerprint | insecure  (only for https:// URLs)
  String fingerprint;       // SHA-1 of the server certificate, for tlsMode=fingerprint
  String adminPass;         // protects settings and OTA; also the Wi-Fi password of the setup AP
  // Correction added to the BME280 temperature. This sensor showed 1.3-1.5 °C too much (22.10.2026);
  // humidity is recomputed by the sensor library with the corrected temperature.
  float tempOffset = -1.4;
  // Correction added to humidity, in percentage points: this sensor showed ~10 % too little (22.10.2026).
  float humOffset = 10;
  // Weather forecast place (Open-Meteo). Name in Latin letters: the LCD has no Cyrillic.
  String fcName = "Babyntsi";
  float fcLat = 50.6331, fcLon = 30.0239;  // смт Бабинці, Бучанський р-н, Київська обл.
  // false = "monitoring mode": LCD and its backlight off, everything else keeps working. Survives power cuts.
  bool displayOn = true;
  // Read-only Healthchecks API key: lets the board read the dashboard's command from the check description.
  String hcRoKey;
  String remoteMode;  // last command seen there ("monitor" / "normal"), so it is applied only once

  bool hasNetworks() const;
};

// Mounts LittleFS (formats it if the flash is blank) and loads the config.
// A missing or broken file gives default settings, never garbage.
void configBegin();
bool configLoad(Config &c);
bool configSave(const Config &c);
void configErase();

// Boot counter: +1 on every start, survives power loss.
uint32_t bootCountIncrement();

// "4 quick power cycles = factory reset".
// Called at boot: returns how many quick boots in a row happened (this one included).
uint8_t fastBootsIncrement();
// Called once the board has run stably for a few seconds.
void fastBootsClear();
