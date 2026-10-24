#include "heartbeat.h"

#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <memory>

#include "golden.h"
#include "ota.h"
#include "sensors.h"
#include "state.h"
#include "trust.h"

// If NTP has not answered yet, certificate dates are checked against this moment
// (21.10.2026, when this firmware was written) so HTTPS still works right after boot.
static const time_t FALLBACK_TIME = 1792540800;


String resetText() {
  static String s;
  if (!s.length()) {
    const rst_info *r = ESP.getResetInfoPtr();
    s = ESP.getResetReason();
    if (r && r->reason == REASON_EXCEPTION_RST) {
      char buf[40];
      snprintf(buf, sizeof(buf), " (%u, epc1 0x%08x)", (unsigned)r->exccause, (unsigned)r->epc1);
      s += buf;
    }
  }
  return s;
}

static String jsonEsc(const String &s) {
  String o;
  for (char ch : s) {
    if (ch == '"' || ch == '\\') o += '\\';
    if ((uint8_t)ch >= 0x20) o += ch;
  }
  return o;
}

static String body() {
  // Healthchecks stores this body; the server reads uptime_s to know when the light came back.
  String b = F("{\"uptime_s\":");
  b += (unsigned long)uptimeSeconds();
  b += F(",\"boot_count\":");
  b += st.bootCount;
  b += F(",\"rssi\":");
  b += WiFi.RSSI();
  b += F(",\"fails\":");
  b += st.failsTotal;
  b += F(",\"heap\":");
  b += ESP.getFreeHeap();
  b += F(",\"blk\":");  // largest free block: HTTPS needs ~16.7 KB in one piece
  b += ESP.getMaxFreeBlockSize();
  // Village temperature / humidity / pressure for the dashboard chart (only when the sensor works)
  if (env.ok) {
    b += F(",\"t\":");
    b += String(env.t, 1);
    if (!isnan(env.h)) {
      b += F(",\"h\":");
      b += String(env.h, 1);
    }
    if (!isnan(env.p)) {
      b += F(",\"p\":");
      b += String(env.p, 1);
    }
  }
  b += F(",\"sbad\":");  // sensor readings thrown away since boot
  b += env.bad;
  b += F(",\"ip\":\"");  // for the link on the dashboard (works only from the same Wi-Fi)
  b += WiFi.localIP().toString();
  b += F("\",\"disp\":");
  b += st.cfg.displayOn ? 1 : 0;
  b += F(",\"sig\":");  // 1 = accepts only signed firmware, remote updates possible
  b += otaSigned() ? 1 : 0;
  if (st.cfg.cmdResult.length()) {
    b += F(",\"cmd\":\"");
    b += jsonEsc(st.cfg.cmdResult);
    b += '"';
  }
  b += F(",\"gold\":\"");  // golden copy version ("" = none), and 1 while this version is on trial
  b += jsonEsc(goldenVersion());
  b += F("\",\"trial\":");
  b += goldenTrial() ? 1 : 0;
  if (otaStatus.length()) {
    b += F(",\"ota\":\"");
    b += jsonEsc(otaStatus);
    b += '"';
  }
  b += F(",\"ap\":\"");  // BSSID the board is connected to, and whether it is the locked one
  b += WiFi.BSSIDstr();
  b += F("\",\"ch\":");
  b += WiFi.channel();
  b += F(",\"rst\":\"");  // why this start happened (an exception shows a crash before it)
  b += jsonEsc(resetText());
  b += F("\",\"fw\":\"" FW_VERSION "\"}");
  return b;
}

bool heartbeatSend() {
  const Config &c = st.cfg;
  st.lastAttemptMs = millis();
  if (!c.url.length()) {
    st.lastCode = 0;
    st.lastError = F("URL не задано");
    return false;
  }

  HTTPClient http;
  std::unique_ptr<WiFiClient> plain;
  std::unique_ptr<BearSSL::WiFiClientSecure> tls;
  bool begun;

  if (c.url.startsWith("https://")) {
    tls.reset(new BearSSL::WiFiClientSecure());
    if (c.tlsMode == "insecure") {
      tls->setInsecure();
    } else if (c.tlsMode == "fingerprint") {
      tls->setFingerprint(c.fingerprint.c_str());
    } else {
      tls->setTrustAnchors(trustSectigo());
      tls->setX509Time(timeValid() ? time(nullptr) : FALLBACK_TIME);
    }
    tls->setTimeout(15000);
    begun = http.begin(*tls, c.url);
  } else {
    plain.reset(new WiFiClient());
    begun = http.begin(*plain, c.url);
  }
  if (!begun) {
    st.lastCode = -100;
    st.lastError = F("неправильний URL");
    st.failsInRow++;
    st.failsTotal++;
    return false;
  }

  http.setTimeout(15000);
  http.setUserAgent(F("svitlo-local/" FW_VERSION));
  http.addHeader(F("Content-Type"), F("application/json"));
  if (c.deviceKey.length()) http.addHeader(F("X-Device-Key"), c.deviceKey);

  Serial.printf("heap %u, largest block %u\n", ESP.getFreeHeap(), ESP.getMaxFreeBlockSize());
  int code = http.POST(body());
  bool ok = code >= 200 && code < 300;
  st.lastCode = code;
  if (ok) {
    st.lastError = "";
    st.lastOkMs = millis();
    st.failsInRow = 0;
    st.sentTotal++;
  } else {
    st.lastError = code < 0 ? http.errorToString(code) : String(F("HTTP ")) + code;
    if (tls) {
      char buf[80];
      int e = tls->getLastSSLError(buf, sizeof(buf));
      if (e) st.lastError += String(F(" / TLS: ")) + buf;
    }
    st.failsInRow++;
    st.failsTotal++;
  }
  http.end();
  Serial.printf("heartbeat %s: %d %s\n", ok ? "ok" : "FAIL", code, st.lastError.c_str());
  return ok;
}

void heartbeatLoop() {
  if (WiFi.status() != WL_CONNECTED) return;
  uint32_t now = millis();
  // Right after connecting send at once; after a failure retry sooner (15 s), not after a full interval.
  uint32_t wait = st.failsInRow ? 15000UL : st.cfg.intervalS * 1000UL;
  if (st.lastAttemptMs && now - st.lastAttemptMs < wait) return;
  heartbeatSend();
}
