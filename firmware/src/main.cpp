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
//
// Factory reset = hold the FLASH button for 10 s while the board runs. It used to be
// "4 quick power cycles", but a flickering grid (auto-reclosing after an accident) can do
// exactly that, and the board would wipe its Wi-Fi settings in the village by itself.
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266WiFiMulti.h>
#include <ESP8266mDNS.h>
#include <Ticker.h>
#include <time.h>

#include "config.h"
#include "display.h"
#include "forecast.h"
#include "golden.h"
#include "ota.h"
#include "remote.h"
#include "heartbeat.h"
#include "sensors.h"
#include "state.h"
#include "trust.h"
#include "web.h"

State st;

static const char *HOSTNAME = "svitlo-local";
static const char *AP_SSID = "SVITLO-LOCAL";
// Kyiv time with summer time; the display shows local time, everything else uses UTC.
static const char *TZ_KYIV = "EET-2EEST,M3.5.0/3,M10.5.0/4";
static const int LED_PIN = 2;  // GPIO2, LED on the ESP-12 module, lit when LOW
static const uint32_t AP_AFTER_MS = 60000;      // no Wi-Fi for this long -> turn the setup AP on
static const uint32_t AP_OFF_AFTER_MS = 120000; // connected for this long and nobody on the AP -> AP off
static const uint32_t LOCK_GRACE_MS = 300000;   // locked access point not seen this long -> any one of the networks
static const int FLASH_PIN = 0;                 // GPIO0 = FLASH button (pressing it at power-on = bootloader)
static const uint32_t RESET_HOLD_MS = 10000;

static ESP8266WiFiMulti multi;
static Ticker loopWatch;
static volatile uint32_t loopSeenMs = 0;
static const uint32_t LOOP_STUCK_MS = 180000;
static uint32_t lastConnectedMs = 0;
static uint32_t connectedSinceMs = 0;
static bool wasConnected = false;

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

bool parseBssid(const String &s, uint8_t out[6]) {
  if (s.length() != 17) return false;
  for (int i = 0; i < 6; i++) {
    if (i < 5 && s[i * 3 + 2] != ':') return false;
    char *end;
    String part = s.substring(i * 3, i * 3 + 2);
    long v = strtol(part.c_str(), &end, 16);
    if (*end || !isxdigit((unsigned char)part[0]) || !isxdigit((unsigned char)part[1])) return false;
    out[i] = (uint8_t)v;
  }
  return true;
}

static int netIndex(const String &ssid) {
  for (int i = 0; i < 3; i++)
    if (st.cfg.nets[i].ssid.length() && st.cfg.nets[i].ssid == ssid) return i;
  return -1;
}

// Waits until the connection succeeds or clearly fails. WiFi.waitForConnectResult() returns at the first
// status other than "disconnected", which right after begin() is often just "idle".
static bool waitConnected(uint32_t ms) {
  for (uint32_t t0 = millis(); millis() - t0 < ms; delay(100)) {
    wl_status_t s = WiFi.status();
    if (s == WL_CONNECTED) return true;
    if (s == WL_WRONG_PASSWORD || s == WL_CONNECT_FAILED) break;
  }
  Serial.printf("Wi-Fi: not connected, status %d\n", (int)WiFi.status());
  return false;
}

// One connection attempt. Scans, then tries the locked access point if it is set and seen, otherwise (or if
// it is not seen for LOCK_GRACE_MS) the access points of the configured networks from the strongest down,
// each pinned to its BSSID and channel, so the SDK does not wander off to a repeater with the same name
// (25.10.2026: next to the router the board sat on a repeater at -80 dBm, and the Wi-Fi stack crashed).
static bool connectBest() {
  static uint32_t lockMissSince = 0;
  uint8_t lock[6];
  bool hasLock = parseBssid(st.cfg.bssidLock, lock);
  // Like ESP8266WiFiMulti: the SDK refuses to scan while it is still trying to connect, so drop that
  // first, and scan asynchronously with a time limit (a synchronous scan waits forever if the SDK
  // never reports the end; 0.8.3 sat without Wi-Fi like that, 25.10.2026).
  WiFi.scanDelete();
  WiFi.disconnect();
  WiFi.scanNetworks(true, false);
  int n = WIFI_SCAN_RUNNING;
  for (uint32_t t0 = millis(); n < 0 && millis() - t0 < 8000; delay(100)) n = WiFi.scanComplete();
  if (n < 0) Serial.printf("Wi-Fi: scan failed (%d)\n", n);
  struct Cand {
    String ssid;
    uint8_t bssid[6];
    int32_t ch, rssi;
    bool locked;
  };
  Cand c[6];
  int nc = 0;
  bool lockedSeen = false;
  String seen;
  for (int i = 0; i < n; i++) {
    if (netIndex(WiFi.SSID(i)) < 0) continue;
    if (seen.length()) seen += F("; ");
    seen += WiFi.SSID(i) + ' ' + WiFi.BSSIDstr(i) + F(" ch ") + WiFi.channel(i) + ' ' + WiFi.RSSI(i) + F(" dBm");
    bool locked = hasLock && !memcmp(WiFi.BSSID(i), lock, 6);
    lockedSeen |= locked;
    if (nc < 6) {
      c[nc].ssid = WiFi.SSID(i);
      memcpy(c[nc].bssid, WiFi.BSSID(i), 6);
      c[nc].ch = WiFi.channel(i);
      c[nc].rssi = locked ? 1000 : WiFi.RSSI(i);  // the locked one sorts first
      c[nc].locked = locked;
      nc++;
    }
  }
  WiFi.scanDelete();
  st.wifiSeen = n < 0 ? String(F("помилка сканування")) : seen.length() ? seen : String(F("жодної точки налаштованих мереж"));
  for (int i = 1; i < nc; i++)  // strongest first
    for (int j = i; j > 0 && c[j].rssi > c[j - 1].rssi; j--) std::swap(c[j], c[j - 1]);
  uint32_t now = millis();
  if (!hasLock || lockedSeen) lockMissSince = 0;
  else if (!lockMissSince) lockMissSince = now | 1;
  bool othersToo = !hasLock || (!lockedSeen && now - lockMissSince > LOCK_GRACE_MS);
  if (hasLock && !lockedSeen) Serial.println(F("Wi-Fi: the locked access point is not seen"));
  for (int i = 0; i < nc; i++) {
    if (!c[i].locked && !othersToo) continue;
    const WifiNet &net = st.cfg.nets[netIndex(c[i].ssid)];
    const uint8_t *b = c[i].bssid;
    Serial.printf("Wi-Fi: %s %02X:%02X:%02X:%02X:%02X:%02X ch %d%s\n", c[i].ssid.c_str(), b[0], b[1], b[2], b[3],
                  b[4], b[5], (int)c[i].ch, c[i].locked ? " (locked)" : "");
    WiFi.begin(c[i].ssid.c_str(), net.pass.c_str(), c[i].ch, c[i].bssid);
    if (waitConnected(15000)) return true;
  }
  return false;
}

static void wifiLoop() {
  uint32_t now = millis();
  if (!st.cfg.hasNetworks()) return;  // only the setup AP

  static uint32_t lastTry = 0;
  bool connected = WiFi.status() == WL_CONNECTED;
  if (!connected && (now - lastTry > 10000 || !lastTry)) {
    lastTry = now;
    if (connectBest()) connected = true;
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

// Factory reset: hold FLASH 10 s, release, then press it once more within 5 s (the LCD says what to do).
// Holding alone is not enough since 0.8.7: on 25.10.2026 the USB adapter kept GPIO0 low after a serial
// port was closed and the board wiped its settings by itself. A stuck line cannot release and press again.
static void resetButtonLoop() {
  static uint32_t downMs = 0, armedMs = 0;
  static bool armed = false;
  bool down = digitalRead(FLASH_PIN) == LOW;
  uint32_t now = millis();
  if (armed) {
    if (now - armedMs > 5000) {  // no second press: forget it
      armed = false;
      displayNote = "";
    } else if (down && !downMs) {
      downMs = now | 1;  // the confirming press
    } else if (!down && downMs) {
      downMs = 0;  // released after a short press: confirmed
      goto doReset;
    } else if (!down) {
      displayNote = F("Press FLASH again");
    }
    return;
  }
  if (!down) {
    if (downMs) {
      uint32_t held = now - downMs;
      downMs = 0;
      displayNote = "";
      if (held >= RESET_HOLD_MS) {
        armed = true;
        armedMs = now;
        displayNote = F("Press FLASH again");
      }
    }
    return;
  }
  if (!downMs) downMs = now | 1;
  {
    uint32_t held = now - downMs;
    if (held < 1000) return;  // a short press does nothing
    displayNote = held < RESET_HOLD_MS ? String(F("Hold ")) + ((RESET_HOLD_MS - held) / 1000 + 1) + F(" s")
                                       : String(F("Release FLASH"));
  }
  return;
doReset:
  Serial.println(F("FLASH held 10 s and pressed again: factory reset"));
  configErase();
  for (int i = 0; i < 20; i++) {  // 2 s of fast blinking as a sign
    digitalWrite(LED_PIN, i % 2 ? HIGH : LOW);
    delay(100);
  }
  ESP.restart();
}

// ---------- main ----------

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  Serial.begin(115200);
  Serial.println(F("\n\nSvitlo-Local ESP " FW_VERSION));

  pinMode(FLASH_PIN, INPUT_PULLUP);
  configBegin();
  trustBegin();  // certificates at the bottom of the heap, before anything else allocates
  goldenBoot();  // as early as possible: counts the starts of a new version, may return to the golden copy
#ifdef SVITLO_TEST_CRASH
  // Test build only (firmware/README): dies right after goldenBoot() to check the return to the golden copy.
  Serial.println(F("SVITLO_TEST_CRASH: crashing on purpose"));
  delay(500);
  *(volatile int *)0 = 1;
#endif
  otaSignatureBegin();  // before anything can install an image (the /update page, a remote update)
  setenv("TZ", TZ_KYIV, 1);
  tzset();
  st.bootCount = bootCountIncrement();
  configLoad(st.cfg);  // before the sensor: it needs the temperature correction
  sensorsBegin();      // DS3231 gives the time right away, before Wi-Fi and NTP
  displayBegin();
  Serial.printf("boot #%u, networks: %s\n", st.bootCount, st.cfg.hasNetworks() ? "yes" : "none");

  WiFi.persistent(false);  // our own config is the only source of truth, not the SDK flash area
  WiFi.setAutoReconnect(true);
  WiFi.hostname(HOSTNAME);
#ifndef SVITLO_TEST_NO_WIFI  // test build only: never connects, to check the 15-minute return to the golden copy
  for (const auto &n : st.cfg.nets)
    if (n.ssid.length()) multi.addAP(n.ssid.c_str(), n.pass.c_str());
#endif

  if (st.cfg.hasNetworks()) {
    WiFi.mode(WIFI_STA);
  } else {
    apStart();
  }
  // 11b/g/n, set explicitly: the SDK keeps the PHY mode in its own flash, so the 11g of 0.8.2 (tried against
  // the SDK beacon crash) stayed even after that line was removed. The village router runs "802.11n only"
  // and does not let a 11g client in, so with 11g the board ended up on the repeater (25.10.2026).
  WiFi.setPhyMode(WIFI_PHY_MODE_11N);
  otaBoot(multi);  // a requested remote update runs now, while the heap is free; on success it restarts
  webBegin();
  // loop() stuck (a call that never returns) = the 15-minute trial check in loop() would never run either.
  // This timer still fires then and restarts the board; 3 such starts of a new version return the golden one.
  loopSeenMs = millis();
  loopWatch.attach(10, [] {
    if (millis() - loopSeenMs > LOOP_STUCK_MS) ESP.restart();
  });
  lastConnectedMs = millis();
}

void loop() {
  loopSeenMs = millis();
  uptimeSeconds();  // keep the 64-bit counter ticking even if nobody asks
  resetButtonLoop();
  wifiLoop();
  webLoop();
  if (WiFi.status() == WL_CONNECTED) MDNS.update();
  sensorsLoop();
  displayLoop();
  heartbeatLoop();
  forecastLoop();
  remoteLoop();
  goldenLoop();
  ledLoop();
}
