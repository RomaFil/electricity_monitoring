#include "config.h"

#include <ArduinoJson.h>
#include <LittleFS.h>

static const char *CONFIG_FILE = "/config.json";
static const char *BOOTS_FILE = "/boot_count";
static const char *FAST_FILE = "/fast_boots";
static const int CONFIG_VERSION = 1;

bool Config::hasNetworks() const {
  for (const auto &n : nets)
    if (n.ssid.length()) return true;
  return false;
}

void configBegin() {
  if (!LittleFS.begin()) {
    // Blank or corrupted flash: start clean instead of reading garbage.
    Serial.println(F("LittleFS mount failed, formatting"));
    LittleFS.format();
    LittleFS.begin();
  }
}

bool configLoad(Config &c) {
  c = Config();
  File f = LittleFS.open(CONFIG_FILE, "r");
  if (!f) return false;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err || doc["v"] != CONFIG_VERSION) {
    Serial.println(F("config invalid, using defaults"));
    c = Config();
    return false;
  }
  JsonArrayConst nets = doc["nets"];
  for (size_t i = 0; i < 3 && i < nets.size(); i++) {
    c.nets[i].ssid = nets[i]["ssid"] | "";
    c.nets[i].pass = nets[i]["pass"] | "";
  }
  c.url = doc["url"] | "";
  c.deviceKey = doc["key"] | "";
  c.intervalS = doc["interval"] | 60;
  c.tlsMode = doc["tls"] | "ca";
  c.fingerprint = doc["fp"] | "";
  c.adminPass = doc["admin"] | "";
  c.tempOffset = doc["toff"] | -1.4f;
  if (c.tempOffset < -10 || c.tempOffset > 10) c.tempOffset = -1.4;
  if (c.intervalS < 30 || c.intervalS > 3600) c.intervalS = 60;
  return true;
}

bool configSave(const Config &c) {
  JsonDocument doc;
  doc["v"] = CONFIG_VERSION;
  JsonArray nets = doc["nets"].to<JsonArray>();
  for (const auto &n : c.nets) {
    JsonObject o = nets.add<JsonObject>();
    o["ssid"] = n.ssid;
    o["pass"] = n.pass;
  }
  doc["url"] = c.url;
  doc["key"] = c.deviceKey;
  doc["interval"] = c.intervalS;
  doc["tls"] = c.tlsMode;
  doc["fp"] = c.fingerprint;
  doc["admin"] = c.adminPass;
  doc["toff"] = c.tempOffset;
  // Write to a temp file first, then rename: a power cut mid-write keeps the old config.
  File f = LittleFS.open("/config.tmp", "w");
  if (!f) return false;
  bool ok = serializeJson(doc, f) > 0;
  f.close();
  return ok && LittleFS.rename("/config.tmp", CONFIG_FILE);
}

void configErase() {
  LittleFS.remove(CONFIG_FILE);
}

static uint32_t readNumber(const char *path) {
  File f = LittleFS.open(path, "r");
  if (!f) return 0;
  uint32_t n = f.readString().toInt();
  f.close();
  return n;
}

static void writeNumber(const char *path, uint32_t n) {
  File f = LittleFS.open(path, "w");
  if (!f) return;
  f.print(n);
  f.close();
}

uint32_t bootCountIncrement() {
  uint32_t n = readNumber(BOOTS_FILE) + 1;
  writeNumber(BOOTS_FILE, n);
  return n;
}

uint8_t fastBootsIncrement() {
  uint32_t n = readNumber(FAST_FILE) + 1;
  writeNumber(FAST_FILE, n);
  return n > 255 ? 255 : n;
}

void fastBootsClear() {
  writeNumber(FAST_FILE, 0);
}
