// Svitlo-Local: ESP8266 (NodeMCU) heartbeat.
//
// Idea: the board is plugged into the same socket as the village router.
// While there is light it pings the server (Healthchecks) once a minute;
// when the light is gone the board is off and the pings stop. Nothing else.
//
// LED (blue, on the ESP-12 module):
//   fast blinking (5 per second)  - no Wi-Fi
//   double blink every 2 s        - only the setup access point SVITLO-LOCAL is on
//   blinking once a second        - Wi-Fi OK, but the last heartbeat failed
//   short flash every 5 s         - everything works
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266WiFiMulti.h>
#include <ESP8266mDNS.h>
#include <time.h>

#include "config.h"
#include "display.h"
#include "forecast.h"
#include "heartbeat.h"
#include "sensors.h"
#include "state.h"
#include "web.h"

State st;

static const char *HOSTNAME = "svitlo-local";
static const char *AP_SSID = "SVITLO-LOCAL";
// Kyiv time with summer time; the display shows local time, everything else uses UTC.
static const char *TZ_KYIV = "EET-2EEST,M3.5.0/3,M10.5.0/4";
static const int LED_PIN = 2;  // GPIO2, LED on the ESP-12 module, lit when LOW
static const uint32_t AP_AFTER_MS = 60000;      // no Wi-Fi for this long -> turn the setup AP on
static const uint32_t AP_OFF_AFTER_MS = 120000; // connected for this long and nobody on the AP -> AP off
static const uint32_t STABLE_MS = 5000;         // after this the "quick power cycles" counter is cleared

static ESP8266WiFiMulti multi;
static uint32_t lastConnectedMs = 0;
static uint32_t connectedSinceMs = 0;
static bool wasConnected = false;
static bool fastCleared = false;

// ---------- helpers shared via state.h ----------

uint64_t uptimeSeconds() {
  static uint32_t last = 0;
  static uint64_t acc = 0;
  uint32_t now = millis();
  acc += (uint32_t)(now - last);  // unsigned difference survives the 49-day wrap
  last = now;
  return acc / 1000;
}

bool timeValid() {
  return time(nullptr) > 1700000000;  // NTP answered (after 2023)
}

String fmtAgo(uint32_t ms) {
  uint32_t s = (millis() - ms) / 1000;
  if (s < 90) return String(s) + F(" с тому");
  if (s < 5400) return String(s / 60) + F(" хв тому");
  return String(s / 3600) + F(" год тому");
}

// ---------- LED ----------

static void ledLoop() {
  uint32_t t = millis();
  bool on;
  bool wifi = WiFi.status() == WL_CONNECTED;
  if (!wifi && st.apActive && !st.cfg.hasNetworks()) {
    uint32_t p = t % 2000;  // double blink
    on = p < 100 || (p > 250 && p < 350);
  } else if (!wifi) {
    on = (t / 100) % 2;  // fast
  } else if (st.lastAttemptMs && st.failsInRow) {
    on = (t / 500) % 2;  // 1 Hz
  } else {
    on = t % 5000 < 50;  // short flash
  }
  digitalWrite(LED_PIN, on ? LOW : HIGH);
}

// ---------- Wi-Fi ----------

static void apStart() {
  if (st.apActive) return;
  WiFi.mode(st.cfg.hasNetworks() ? WIFI_AP_STA : WIFI_AP);
  // Open AP only for the very first setup; afterwards the admin password protects it.
  const char *pass = st.cfg.adminPass.length() >= 8 ? st.cfg.adminPass.c_str() : nullptr;
  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1), IPAddress(255, 255, 255, 0));
  WiFi.softAP(AP_SSID, pass);
  webCaptive(true);
  st.apActive = true;
  Serial.println(F("setup AP SVITLO-LOCAL on, http://192.168.4.1/"));
}

static void apStop() {
  if (!st.apActive) return;
  webCaptive(false);
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  st.apActive = false;
  Serial.println(F("setup AP off"));
}

static void onConnected() {
  Serial.printf("Wi-Fi: %s, IP %s, RSSI %d\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
  configTime(TZ_KYIV, "pool.ntp.org", "time.google.com");  // NTP also corrects the DS3231
  MDNS.end();
  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);
}

static void wifiLoop() {
  uint32_t now = millis();
  if (!st.cfg.hasNetworks()) return;  // only the setup AP

  static uint32_t lastTry = 0;
  bool connected = WiFi.status() == WL_CONNECTED;
  if (!connected && (now - lastTry > 10000 || !lastTry)) {
    lastTry = now;
    // Tries the networks it can see, strongest of the configured ones first; short timeout so
    // the web page and LED keep working.
    if (multi.run(4000) == WL_CONNECTED) connected = true;
  }

  if (connected && !wasConnected) {
    connectedSinceMs = now;
    onConnected();
  }
  if (!connected && wasConnected) Serial.println(F("Wi-Fi lost"));
  wasConnected = connected;

  if (connected) {
    lastConnectedMs = now;
    if (st.apActive && now - connectedSinceMs > AP_OFF_AFTER_MS && WiFi.softAPgetStationNum() == 0) apStop();
  } else if (!st.apActive && now - lastConnectedMs > AP_AFTER_MS) {
    apStart();  // still keeps trying the networks in the background (AP+STA)
  }
}

// ---------- main ----------

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  Serial.begin(115200);
  Serial.println(F("\n\nSvitlo-Local ESP " FW_VERSION));

  configBegin();
  uint8_t fast = fastBootsIncrement();
  if (fast >= 4) {
    Serial.println(F("4 quick power cycles: factory reset"));
    configErase();
    fastBootsClear();
    for (int i = 0; i < 20; i++) {  // 2 s of fast blinking as a sign
      digitalWrite(LED_PIN, i % 2 ? HIGH : LOW);
      delay(100);
    }
  }
  setenv("TZ", TZ_KYIV, 1);
  tzset();
  st.bootCount = bootCountIncrement();
  configLoad(st.cfg);  // before the sensor: it needs the temperature correction
  sensorsBegin();      // DS3231 gives the time right away, before Wi-Fi and NTP
  displayBegin();
  Serial.printf("boot #%u, quick boots %u, networks: %s\n", st.bootCount, fast, st.cfg.hasNetworks() ? "yes" : "none");

  WiFi.persistent(false);  // our own config is the only source of truth, not the SDK flash area
  WiFi.setAutoReconnect(true);
  WiFi.hostname(HOSTNAME);
  for (const auto &n : st.cfg.nets)
    if (n.ssid.length()) multi.addAP(n.ssid.c_str(), n.pass.c_str());

  if (st.cfg.hasNetworks()) {
    WiFi.mode(WIFI_STA);
  } else {
    apStart();
  }
  webBegin();
  lastConnectedMs = millis();
}

void loop() {
  uptimeSeconds();  // keep the 64-bit counter ticking even if nobody asks
  if (!fastCleared && millis() > STABLE_MS) {
    fastBootsClear();
    fastCleared = true;
  }
  wifiLoop();
  webLoop();
  if (WiFi.status() == WL_CONNECTED) MDNS.update();
  sensorsLoop();
  displayLoop();
  heartbeatLoop();
  forecastLoop();
  ledLoop();
}
