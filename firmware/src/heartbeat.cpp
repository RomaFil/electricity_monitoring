#include "heartbeat.h"

#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <memory>

#include "certs.h"
#include "state.h"

// If NTP has not answered yet, certificate dates are checked against this moment
// (21.10.2026, when this firmware was written) so HTTPS still works right after boot.
static const time_t FALLBACK_TIME = 1792540800;

static BearSSL::X509List *trustAnchor() {
  static BearSSL::X509List *ta = nullptr;
  if (!ta) ta = new BearSSL::X509List(ROOT_CA_SECTIGO_E46);
  return ta;
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
  b += F(",\"fw\":\"" FW_VERSION "\"}");
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
      tls->setTrustAnchors(trustAnchor());
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
