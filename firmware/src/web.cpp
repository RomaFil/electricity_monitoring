#include "web.h"

#include <DNSServer.h>
#include <ESP8266HTTPUpdateServer.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>

#include "config.h"
#include "display.h"
#include "heartbeat.h"
#include "sensors.h"
#include "state.h"

static ESP8266WebServer server(80);
static ESP8266HTTPUpdateServer updater;
static DNSServer dns;
static bool dnsOn = false;
static const char *ADMIN_USER = "admin";

static String esc(const String &s) {
  String o;
  o.reserve(s.length() + 8);
  for (char ch : s) {
    switch (ch) {
      case '&': o += F("&amp;"); break;
      case '<': o += F("&lt;"); break;
      case '>': o += F("&gt;"); break;
      case '"': o += F("&quot;"); break;
      default: o += ch;
    }
  }
  return o;
}

// The ping URL contains the Healthchecks UUID, which is a secret: show only the host.
static String maskedUrl(const String &url) {
  int p = url.indexOf("://");
  if (p < 0) return F("(не задано)");
  int slash = url.indexOf('/', p + 3);
  return url.substring(0, slash < 0 ? url.length() : slash) + (slash < 0 ? "" : "/…");
}

static const char HEAD[] PROGMEM = R"HTML(<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Svitlo-Local ESP</title>
<style>body{margin:0;background:#111113;color:#ececf0;font:15px/1.45 system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px}h1{font-size:20px}h2{font-size:16px;margin:18px 0 6px}
.card{background:#1b1b1f;border:1px solid #2e2e35;border-radius:12px;padding:12px 14px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}td{padding:4px 2px;border-top:1px solid #2e2e35;vertical-align:top}
td:first-child{color:#9a9aa3;width:45%}.ok{color:#4ed164}.bad{color:#ff6150}
label{display:block;margin:8px 0 2px;color:#9a9aa3;font-size:13px}
input,select{width:100%;box-sizing:border-box;padding:8px;border-radius:8px;border:1px solid #3a3a42;background:#232328;color:#ececf0;font:inherit}
button,.btn{display:inline-block;margin-top:12px;padding:9px 16px;border:0;border-radius:8px;background:#38b6ff;color:#0b0b0d;font:inherit;font-weight:600;text-decoration:none;cursor:pointer}
.danger{background:#ff6150}.muted{color:#9a9aa3;font-size:13px}a{color:#38b6ff}</style></head><body><main>)HTML";
static const char TAIL[] PROGMEM = "</main></body></html>";

static void sendPage(const String &body, int code = 200) {
  String page = FPSTR(HEAD);
  page += body;
  page += FPSTR(TAIL);
  server.send(code, F("text/html; charset=utf-8"), page);
}

// Settings need the admin password once it is set. Before that (first setup) the page is open,
// and the form refuses to save without a new password.
static bool authOk() {
  if (!st.cfg.adminPass.length()) return true;
  if (server.authenticate(ADMIN_USER, st.cfg.adminPass.c_str())) return true;
  server.requestAuthentication(BASIC_AUTH, "Svitlo-Local", F("Потрібен пароль адміністратора"));
  return false;
}

static String row(const __FlashStringHelper *k, const String &v) {
  return String(F("<tr><td>")) + k + F("</td><td>") + v + F("</td></tr>");
}

static void handleStatus() {
  bool wifi = WiFi.status() == WL_CONNECTED;
  String b = F("<h1>Svitlo-Local ESP</h1><div class=card><table>");
  b += row(F("Аптайм"), String((unsigned long)uptimeSeconds()) + F(" с"));
  b += row(F("Wi-Fi"), wifi ? String(F("<span class=ok>")) + esc(WiFi.SSID()) + F("</span>, ") + WiFi.RSSI() + F(" dBm")
                            : String(F("<span class=bad>не підключено</span>")));
  b += row(F("IP"), wifi ? WiFi.localIP().toString() : String("—"));
  if (st.apActive) b += row(F("Точка доступу"), F("SVITLO-LOCAL, 192.168.4.1"));
  b += row(F("Сервер"), esc(maskedUrl(st.cfg.url)));
  String hb;
  if (!st.lastAttemptMs) hb = F("ще не було");
  else if (st.lastCode >= 200 && st.lastCode < 300) hb = String(F("<span class=ok>OK</span>, ")) + fmtAgo(st.lastAttemptMs);
  else hb = String(F("<span class=bad>")) + esc(st.lastError) + F("</span>, ") + fmtAgo(st.lastAttemptMs);
  b += row(F("Останній heartbeat"), hb);
  b += row(F("Останній успішний"), st.lastOkMs ? fmtAgo(st.lastOkMs) : String(F("ще не було")));
  b += row(F("Невдач підряд / всього"), String(st.failsInRow) + " / " + st.failsTotal);
  b += row(F("Надіслано"), String(st.sentTotal));
  b += row(F("Перезапусків (boot count)"), String(st.bootCount));
  String clock;
  if (timeSource == TimeSource::None) clock = F("<span class=bad>невідомий</span>");
  else {
    time_t now = time(nullptr);
    tm lt;
    localtime_r(&now, &lt);
    char buf[24];
    strftime(buf, sizeof(buf), "%d.%m.%Y %H:%M:%S", &lt);
    clock = String(buf) + (timeSource == TimeSource::Ntp ? F(" (NTP)") : F(" (з DS3231)"));
  }
  b += row(F("Час"), clock);
  b += row(F("Годинник DS3231"), !rtcFound ? String(F("<span class=bad>не знайдено</span>"))
                                 : rtcLostPower ? String(F("<span class=bad>знайдено, час втрачено (батарейка?)</span>"))
                                                : String(F("<span class=ok>працює</span>")));
  String sensor;
  if (!env.sensor.length()) sensor = F("<span class=bad>не знайдено</span>");
  else if (!env.ok) sensor = env.sensor + F(", <span class=bad>немає показів</span>");
  else {
    sensor = env.sensor + F(": ") + String(env.t, 1) + F(" °C");
    if (!isnan(env.h)) sensor += F(", ") + String(env.h, 0) + F(" %");
    if (!isnan(env.p)) sensor += F(", ") + String(env.p, 0) + F(" гПа");
    if (env.sensor == "BMP280") sensor += F(" <span class=muted>(BMP280 не міряє вологість)</span>");
  }
  b += row(F("Датчик"), sensor);
  b += row(F("Шина I²C"), i2cScan());
  b += row(F("Дисплей"), esc(displayInfo));
  b += row(F("Вільна пам'ять"), String(ESP.getFreeHeap()) + F(" байт"));
  b += row(F("Прошивка"), F(FW_VERSION));
  b += F("</table></div><a class=btn href=/settings>Налаштування</a> ");
  if (st.cfg.adminPass.length()) b += F("<a class=btn href=/update>Оновити прошивку</a>");
  sendPage(b);
}

static void handleApiStatus() {
  String j = F("{\"uptime_s\":");
  j += (unsigned long)uptimeSeconds();
  j += F(",\"wifi\":");
  j += WiFi.status() == WL_CONNECTED ? F("true") : F("false");
  j += F(",\"rssi\":");
  j += WiFi.RSSI();
  j += F(",\"last_code\":");
  j += st.lastCode;
  j += F(",\"fails_in_row\":");
  j += st.failsInRow;
  j += F(",\"boot_count\":");
  j += st.bootCount;
  j += F(",\"fw\":\"" FW_VERSION "\"}");
  server.send(200, F("application/json"), j);
}

static void handleSettingsGet() {
  if (!authOk()) return;
  const Config &c = st.cfg;
  // Visible networks as hints for the SSID fields (takes ~2 s).
  int n = WiFi.scanNetworks();
  String b = F("<h1>Налаштування</h1><form method=post action=/settings><datalist id=ssids>");
  for (int i = 0; i < n; i++) b += String(F("<option value=\"")) + esc(WiFi.SSID(i)) + F("\">");
  WiFi.scanDelete();
  b += F("</datalist>");
  const char *names[3] = {"Основна мережа Wi-Fi", "Резервна мережа 1", "Резервна мережа 2"};
  for (int i = 0; i < 3; i++) {
    b += String(F("<div class=card><h2>")) + names[i] + F("</h2><label>Назва (SSID)</label><input list=ssids name=s") + i +
         F(" value=\"") + esc(c.nets[i].ssid) + F("\"><label>Пароль</label><input type=password name=p") + i +
         F(" placeholder=\"") + (c.nets[i].pass.length() ? F("без змін") : F("")) + F("\"></div>");
  }
  b += F("<div class=card><h2>Сервер</h2><label>URL (https://hc-ping.com/&lt;UUID&gt; або http://сервер/api/heartbeat)</label>"
         "<input name=url value=\"");
  b += esc(c.url);
  b += F("\"><label>Ключ пристрою X-Device-Key (лише для прямого режиму)</label><input type=password name=key placeholder=\"");
  b += c.deviceKey.length() ? F("без змін") : F("не потрібен для Healthchecks");
  b += F("\"><label>Інтервал heartbeat, секунд (30–3600)</label><input type=number min=30 max=3600 name=interval value=");
  b += c.intervalS;
  b += F("><label>Перевірка HTTPS</label><select name=tls>");
  const char *modes[3][2] = {{"ca", "кореневий сертифікат Sectigo (рекомендовано)"},
                             {"fingerprint", "відбиток сертифіката сайту (SHA-1)"},
                             {"insecure", "без перевірки (не рекомендовано)"}};
  for (auto &m : modes)
    b += String(F("<option value=")) + m[0] + (c.tlsMode == m[0] ? F(" selected>") : F(">")) + m[1] + F("</option>");
  b += F("</select><label>Відбиток SHA-1 (для режиму «відбиток»)</label><input name=fp value=\"");
  b += esc(c.fingerprint);
  b += F("\" placeholder=\"AA:BB:...\"></div><div class=card><h2>Датчик</h2>"
         "<label>Поправка температури, °C (від’ємна — якщо датчик показує більше, ніж є)</label>"
         "<input type=number step=0.1 min=-10 max=10 name=toff value=");
  b += String(c.tempOffset, 1);
  b += F("><label>Поправка вологості, % (додатна — якщо датчик показує менше, ніж є)</label>"
         "<input type=number step=1 min=-30 max=30 name=hoff value=");
  b += String(c.humOffset, 0);
  b += F("></div><div class=card><h2>Пароль адміністратора</h2>"
         "<p class=muted>Захищає налаштування й оновлення прошивки; також пароль точки доступу SVITLO-LOCAL. Мінімум 8 символів.</p>"
         "<label>Новий пароль</label><input type=password name=admin minlength=8 placeholder=\"");
  b += c.adminPass.length() ? F("без змін") : F("обов'язково задати");
  b += F("\"></div><button>Зберегти й перезавантажити</button></form>"
         "<form method=post action=/reset onsubmit=\"return confirm('Стерти всі налаштування?')\">"
         "<button class=danger>Скинути до заводських</button></form><p><a href=/>← статус</a></p>");
  sendPage(b);
}

static void handleSettingsPost() {
  if (!authOk()) return;
  Config c = st.cfg;
  for (int i = 0; i < 3; i++) {
    c.nets[i].ssid = server.arg(String("s") + i);
    c.nets[i].ssid.trim();
    String p = server.arg(String("p") + i);
    if (p.length()) c.nets[i].pass = p;
    if (!c.nets[i].ssid.length()) c.nets[i].pass = "";
  }
  String url = server.arg("url");
  url.trim();
  String key = server.arg("key");
  if (key.length()) c.deviceKey = key;
  long interval = server.arg("interval").toInt();
  c.tlsMode = server.arg("tls");
  if (c.tlsMode != "fingerprint" && c.tlsMode != "insecure") c.tlsMode = "ca";
  c.fingerprint = server.arg("fp");
  c.fingerprint.trim();
  String admin = server.arg("admin");
  if (server.hasArg("toff")) c.tempOffset = constrain(server.arg("toff").toFloat(), -10.0f, 10.0f);
  if (server.hasArg("hoff")) c.humOffset = constrain(server.arg("hoff").toFloat(), -30.0f, 30.0f);

  String err;
  if (url.length() && !url.startsWith("http://") && !url.startsWith("https://")) err = F("URL має починатися з http:// або https://");
  else if (interval < 30 || interval > 3600) err = F("Інтервал має бути від 30 до 3600 секунд");
  else if (admin.length() && admin.length() < 8) err = F("Пароль адміністратора — щонайменше 8 символів");
  else if (!admin.length() && !c.adminPass.length()) err = F("Задайте пароль адміністратора");
  else if (c.tlsMode == "fingerprint" && c.fingerprint.length() < 40) err = F("Для режиму «відбиток» потрібен SHA-1 відбиток");
  if (err.length()) {
    sendPage(String(F("<h1>Не збережено</h1><p class=bad>")) + err + F("</p><p><a href=/settings>← назад</a></p>"), 400);
    return;
  }
  c.url = url;
  c.intervalS = interval;
  if (admin.length()) c.adminPass = admin;
  if (!configSave(c)) {
    sendPage(F("<h1>Помилка запису у flash</h1>"), 500);
    return;
  }
  sendPage(F("<h1>Збережено</h1><p>Плата перезавантажується. Якщо вона підключиться до вашої мережі, сторінка буде "
             "на http://svitlo-local.local/ або за IP з роутера.</p>"));
  delay(1000);
  ESP.restart();
}

static void handleReset() {
  if (!authOk()) return;
  configErase();
  sendPage(F("<h1>Налаштування стерто</h1><p>Після перезавантаження з'явиться точка доступу SVITLO-LOCAL (без пароля).</p>"));
  delay(1000);
  ESP.restart();
}

static void handleNotFound() {
  if (dnsOn) {
    // Captive portal: phones open any address -> send them to the settings page.
    server.sendHeader(F("Location"), F("http://192.168.4.1/settings"), true);
    server.send(302, F("text/plain"), "");
    return;
  }
  server.send(404, F("text/plain"), F("not found"));
}

void webBegin() {
  server.on("/", HTTP_GET, handleStatus);
  server.on("/api/status", HTTP_GET, handleApiStatus);
  server.on("/settings", HTTP_GET, handleSettingsGet);
  server.on("/settings", HTTP_POST, handleSettingsPost);
  server.on("/reset", HTTP_POST, handleReset);
  server.onNotFound(handleNotFound);
  // OTA only with a password; without it anyone in the network could flash the board.
  if (st.cfg.adminPass.length()) updater.setup(&server, "/update", ADMIN_USER, st.cfg.adminPass.c_str());
  server.begin();
}

void webCaptive(bool on) {
  if (on && !dnsOn) dnsOn = dns.start(53, "*", IPAddress(192, 168, 4, 1));
  if (!on && dnsOn) {
    dns.stop();
    dnsOn = false;
  }
}

void webLoop() {
  if (dnsOn) dns.processNextRequest();
  server.handleClient();
}
