#include "remote.h"

#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <bearssl/bearssl.h>
#include <memory>

#include "config.h"
#include "heartbeat.h"
#include "golden.h"
#include "ota.h"
#include "sensors.h"
#include "state.h"
#include "trust.h"

String remoteStatus;
static const uint32_t EVERY_MS = 120000;
static const uint32_t FIRST_DELAY_MS = 30000;

void setDisplayOn(bool on) {
  if (st.cfg.displayOn == on) return;
  st.cfg.displayOn = on;
  configSave(st.cfg);
  Serial.printf("display %s\n", on ? "on" : "off (monitoring mode)");
}

// "https://hc-ping.com/<uuid>" -> "<uuid>"; "" if the board does not ping Healthchecks
static String checkUuid() {
  const String &u = st.cfg.url;
  if (u.indexOf("hc-ping.com/") < 0) return "";
  return u.substring(u.lastIndexOf('/') + 1);
}

// "svitlo-mode=monitor#1792800000": display on/off
static void applyMode(const String &desc) {
  int i = desc.indexOf("svitlo-mode=");
  if (i < 0) {
    remoteStatus += F(", режиму ще не задавали");
    return;
  }
  // token: "monitor#1792800000" — the number is the time of the button press, so pressing the same
  // button again after a local switch on the ESP page still counts as a new command
  String token = desc.substring(i + 12);
  int end = 0;
  while (end < (int)token.length() && (isalnum(token[end]) || token[end] == '#')) end++;
  token = token.substring(0, end);
  String mode = token.substring(0, token.indexOf('#') < 0 ? token.length() : token.indexOf('#'));
  remoteStatus += String(F(", режим: ")) + token;
  if (mode != "monitor" && mode != "normal") return;
  if (token == st.cfg.remoteMode) return;  // already applied; a later local switch is not overridden

  st.cfg.remoteMode = token;
  st.cfg.displayOn = mode != "monitor";
  configSave(st.cfg);
  Serial.printf("remote: mode %s from the dashboard\n", mode.c_str());
}

// HMAC-SHA256(cmdKey, message), first 16 bytes as 32 lowercase hex digits (server: healthchecks.sign_cmd)
static String hmacHex(const String &key, const String &msg) {
  br_hmac_key_context kc;
  br_hmac_key_init(&kc, &br_sha256_vtable, key.c_str(), key.length());
  br_hmac_context ctx;
  br_hmac_init(&ctx, &kc, 0);
  br_hmac_update(&ctx, msg.c_str(), msg.length());
  uint8_t out[32];
  br_hmac_out(&ctx, out);
  static const char HEX_DIGITS[] = "0123456789abcdef";
  String h;
  h.reserve(32);
  for (int i = 0; i < 16; i++) {
    h += HEX_DIGITS[out[i] >> 4];
    h += HEX_DIGITS[out[i] & 15];
  }
  return h;
}

static bool sameText(const String &a, const String &b) {  // constant time: no hint how much of a guess was right
  if (a.length() != b.length()) return false;
  uint8_t d = 0;
  for (size_t i = 0; i < a.length(); i++) d |= a[i] ^ b[i];
  return d == 0;
}

static void finish(const String &cmd, uint32_t ts, const String &result, bool restart) {
  st.cfg.cmdTs = ts;
  st.cfg.cmdResult = cmd + '#' + String(ts) + ' ' + result;
  configSave(st.cfg);  // before restarting: the same command must never run twice
  Serial.printf("remote: %s\n", st.cfg.cmdResult.c_str());
  if (!restart) return;
  heartbeatSend();  // fresh ping with the result right now: the restart then makes no gap the server notices
  delay(200);
  ESP.restart();
}

// "svitlo-cmd=<command>#<unix time>#<hmac>", signed by the VPS with the key both sides know.
// Whoever can only edit the check description (a stolen full API key) cannot forge it, and a
// command that was already executed (same or older time) is never repeated.
static void applyCommand(const String &desc) {
  int i = desc.indexOf("svitlo-cmd=");
  if (i < 0) return;
  String token = desc.substring(i + 11);
  int end = 0;
  while (end < (int)token.length() && (isalnum(token[end]) || strchr("#:.", token[end]))) end++;
  token = token.substring(0, end);
  int a = token.indexOf('#'), b = token.indexOf('#', a + 1);
  if (a <= 0 || b < 0) {
    remoteStatus += F(", команда: неправильний формат");
    return;
  }
  String cmd = token.substring(0, a);
  uint32_t ts = strtoul(token.substring(a + 1, b).c_str(), nullptr, 10);
  String sig = token.substring(b + 1);
  if (ts <= st.cfg.cmdTs) return;  // done already (cmdResult shows it)
  if (!st.cfg.cmdKey.length()) {
    remoteStatus += F(", команда: на платі не задано ключ команд");
    return;
  }
  if (!sameText(hmacHex(st.cfg.cmdKey, cmd + '#' + String(ts)), sig)) {
    remoteStatus += F(", команда: підпис не збігся (інший ключ команд?)");
    return;  // cmdTs stays: a forged command must not block the real ones
  }
  if (!timeValid()) {
    remoteStatus += F(", команда: чекаю точний час (NTP)");
    return;  // tried again at the next check
  }
  time_t now = time(nullptr);
  if ((time_t)ts + 86400 < now || (time_t)ts > now + 600) return finish(cmd, ts, F("застаріла, не виконано"), false);

  if (cmd == "reboot") return finish(cmd, ts, F("перезапуск"), true);
  if (cmd == "sensor") {
    bool ok = sensorsReprobe();
    return finish(cmd, ts, ok ? String(F("датчик перечитано: ")) + String(env.t, 1) + F(" °C")
                              : String(F("датчик не відповідає")), false);
  }
  if (cmd == "golden") return finish(cmd, ts, goldenSave(), false);
  if (cmd.startsWith("ota:")) {
    String ver = cmd.substring(4);
    if (!otaSigned()) return finish(cmd, ts, F("відхилено: ця прошивка не перевіряє підпис"), false);
    if (!otaVersionOk(ver)) return finish(cmd, ts, F("відхилено: неправильна версія"), false);
    if (ver == FW_VERSION) return finish(cmd, ts, F("ця версія вже стоїть"), false);
    if (!otaRequest(ver)) return finish(cmd, ts, F("помилка запису у flash"), false);
    return finish(cmd, ts, F("перезапуск і завантаження з GitHub"), true);
  }
  finish(cmd, ts, F("невідома команда"), false);
}

static void poll() {
  String uuid = checkUuid();
  if (!uuid.length() || !st.cfg.hcRoKey.length()) {
    remoteStatus = !uuid.length() ? F("вимкнено: URL не hc-ping.com") : F("вимкнено: немає read-only ключа");
    return;
  }

  std::unique_ptr<BearSSL::WiFiClientSecure> tls(new BearSSL::WiFiClientSecure());
  tls->setTrustAnchors(trustSectigo());  // healthchecks.io has the same Sectigo chain as hc-ping.com
  if (timeValid()) tls->setX509Time(time(nullptr));
  tls->setTimeout(15000);

  HTTPClient http;
  http.useHTTP10(true);
  if (!http.begin(*tls, String(F("https://healthchecks.io/api/v3/checks/")) + uuid)) {
    remoteStatus = F("помилка: неправильна адреса");
    return;
  }
  http.addHeader(F("X-Api-Key"), st.cfg.hcRoKey);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("remote: HTTP %d\n", code);
    remoteStatus = String(F("помилка: ")) + (code < 0 ? http.errorToString(code) : String(F("HTTP ")) + code);
    if (code == 401 || code == 403) remoteStatus += F(" (ключ не підходить до цієї перевірки?)");
    char buf[80];
    if (code < 0 && tls->getLastSSLError(buf, sizeof(buf))) remoteStatus += String(F(" / TLS: ")) + buf;
    http.end();
    return;
  }
  JsonDocument filter;
  filter["desc"] = true;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();
  if (err) {
    remoteStatus = String(F("помилка JSON: ")) + err.c_str();
    return;
  }
  remoteStatus = F("OK");

  String desc = doc["desc"] | "";
  applyMode(desc);
  applyCommand(desc);  // last: a command may restart the board
}

void remoteLoop() {
  static uint32_t nextMs = FIRST_DELAY_MS;
  static uint32_t lastMs = 0;
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED || (int32_t)(now - nextMs) < 0) return;
  nextMs = now + EVERY_MS;
  lastMs = now;
  poll();
  (void)lastMs;
}
