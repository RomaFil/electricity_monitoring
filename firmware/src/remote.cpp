#include "remote.h"

#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <memory>

#include "certs.h"
#include "config.h"
#include "state.h"

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

static void poll() {
  String uuid = checkUuid();
  if (!uuid.length() || !st.cfg.hcRoKey.length()) return;

  static BearSSL::X509List *ta = nullptr;  // healthchecks.io has the same Sectigo chain as hc-ping.com
  if (!ta) ta = new BearSSL::X509List(ROOT_CA_SECTIGO_E46);
  std::unique_ptr<BearSSL::WiFiClientSecure> tls(new BearSSL::WiFiClientSecure());
  tls->setTrustAnchors(ta);
  if (timeValid()) tls->setX509Time(time(nullptr));
  tls->setTimeout(15000);

  HTTPClient http;
  http.useHTTP10(true);
  if (!http.begin(*tls, String(F("https://healthchecks.io/api/v3/checks/")) + uuid)) return;
  http.addHeader(F("X-Api-Key"), st.cfg.hcRoKey);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("remote: HTTP %d\n", code);
    http.end();
    return;
  }
  JsonDocument filter;
  filter["desc"] = true;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();
  if (err) return;

  String desc = doc["desc"] | "";
  int i = desc.indexOf("svitlo-mode=");
  if (i < 0) return;
  // token: "monitor#1792800000" — the number is the time of the button press, so pressing the same
  // button again after a local switch on the ESP page still counts as a new command
  String token = desc.substring(i + 12);
  int end = 0;
  while (end < (int)token.length() && (isalnum(token[end]) || token[end] == '#')) end++;
  token = token.substring(0, end);
  String mode = token.substring(0, token.indexOf('#') < 0 ? token.length() : token.indexOf('#'));
  if (mode != "monitor" && mode != "normal") return;
  if (token == st.cfg.remoteMode) return;  // already applied; a later local switch is not overridden

  st.cfg.remoteMode = token;
  st.cfg.displayOn = mode != "monitor";
  configSave(st.cfg);
  Serial.printf("remote: mode %s from the dashboard\n", mode.c_str());
}

void remoteLoop() {
  static uint32_t nextMs = FIRST_DELAY_MS;
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED || (int32_t)(now - nextMs) < 0) return;
  nextMs = now + EVERY_MS;
  poll();
}
