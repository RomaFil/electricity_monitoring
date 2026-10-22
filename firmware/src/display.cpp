#include "display.h"

#include <ESP8266WiFi.h>
#include <Wire.h>
#include <time.h>

#include "sensors.h"
#include "state.h"

// ESP8266 has one software I2C engine; we point it at the LCD pins for a moment and back.
static const uint8_t LCD_SDA = 12, LCD_SCL = 14;    // D6, D5
static const uint8_t MAIN_SDA = 4, MAIN_SCL = 5;    // D2, D1 (BME280, DS3231)

// PCF8574 pins -> HD44780: P0=RS, P1=RW, P2=EN, P3=backlight, P4..P7=D4..D7 (the usual backpack wiring)
static const uint8_t RS = 0x01, EN = 0x04, BACKLIGHT = 0x08;

String displayInfo = "LCD не знайдено";
static uint8_t addr = 0;           // 0 = no LCD
static bool busOk = true;          // false after a failed write -> look for the LCD again
static String shown[2];            // what is on the screen now, to rewrite only changes
static uint32_t lastProbeMs = 0;

static void lcdBus() { Wire.begin(LCD_SDA, LCD_SCL); }
static void mainBus() { Wire.begin(MAIN_SDA, MAIN_SCL); }

static void expanderWrite(uint8_t v) {
  Wire.beginTransmission(addr);
  Wire.write(v | BACKLIGHT);
  if (Wire.endTransmission() != 0) busOk = false;
}

static void nibble(uint8_t v) {  // v: high 4 bits = data, low bits = RS
  expanderWrite(v);
  expanderWrite(v | EN);  // the LCD reads the data on the falling edge of EN
  delayMicroseconds(1);
  expanderWrite(v & ~EN);
  delayMicroseconds(50);
}

static void send(uint8_t value, uint8_t mode) {
  nibble((value & 0xF0) | mode);
  nibble(((value << 4) & 0xF0) | mode);
}

static void command(uint8_t c) { send(c, 0); }

static bool findLcd() {
  // PCF8574 answers at 0x20..0x27, PCF8574A at 0x38..0x3F (set by the A0..A2 jumpers)
  for (uint8_t a = 0x20; a <= 0x3F; a++) {
    if (a > 0x27 && a < 0x38) continue;
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      addr = a;
      return true;
    }
  }
  addr = 0;
  return false;
}

static void lcdInit() {
  // HD44780 "initialisation by instruction" for 4-bit mode (datasheet, figure 24)
  busOk = true;
  expanderWrite(0);
  delay(50);
  nibble(0x30);
  delay(5);
  nibble(0x30);
  delay(5);
  nibble(0x30);
  delayMicroseconds(150);
  nibble(0x20);    // 4-bit
  command(0x28);   // 2 lines, 5x8 font
  command(0x0C);   // display on, no cursor
  command(0x01);   // clear
  delay(2);
  command(0x06);   // left to right
  // Own characters in CGRAM slots 0..5. They are printed as codes 8..13 (the LCD maps 8..15 to the
  // same slots), so a String never has to contain a 0 byte.
  static const uint8_t GLYPHS[6][8] = {
      {0x00, 0x00, 0x00, 0x0E, 0x0E, 0x0E, 0x00, 0x00},  // 0: Wi-Fi dot
      {0x1F, 0x1F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00},  // 1: bar at the top
      {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F, 0x1F},  // 2: bar at the bottom
      {0x1F, 0x1F, 0x00, 0x00, 0x00, 0x00, 0x1F, 0x1F},  // 3: bars top and bottom
      {0x00, 0x00, 0x00, 0x00, 0x0E, 0x0E, 0x0E, 0x00},  // 4: upper colon dot (low in the top row)
      {0x00, 0x0E, 0x0E, 0x0E, 0x00, 0x00, 0x00, 0x00},  // 5: lower colon dot (high in the bottom row)
  };
  command(0x40);
  for (auto &g : GLYPHS)
    for (uint8_t b : g) send(b, RS);
  command(0x80);
  shown[0] = shown[1] = "";
}

static void writeLine(uint8_t row, String text) {
  while (text.length() < 16) text += ' ';
  text = text.substring(0, 16);
  if (text == shown[row]) return;
  command(0x80 | (row ? 0x40 : 0x00));  // cursor to the line start
  for (size_t i = 0; i < 16; i++) send((uint8_t)text[i], RS);
  shown[row] = text;
}

static const char WIFI_DOT = 8, TOP = 9, BOT = 10, BOTH = 11, COLON_UP = 12, COLON_DN = 13;
static const char FULL = (char)0xFF;  // full block, built into the LCD font
static const char DEG = (char)0xDF;   // degree sign, built into the LCD font

// Big digits: 3 columns x 2 rows each. A "middle" line is drawn as the bottom bar of the top row.
static const char BIG[11][2][4] = {
    {{FULL, TOP, FULL, 0}, {FULL, BOT, FULL, 0}},     // 0
    {{TOP, FULL, ' ', 0}, {BOT, FULL, BOT, 0}},       // 1
    {{BOTH, BOTH, FULL, 0}, {FULL, BOT, BOT, 0}},     // 2
    {{BOTH, BOTH, FULL, 0}, {BOT, BOT, FULL, 0}},     // 3
    {{FULL, BOT, FULL, 0}, {' ', ' ', FULL, 0}},      // 4
    {{FULL, BOTH, BOTH, 0}, {BOT, BOT, FULL, 0}},     // 5
    {{FULL, BOTH, BOTH, 0}, {FULL, BOT, FULL, 0}},    // 6
    {{TOP, TOP, FULL, 0}, {' ', ' ', FULL, 0}},       // 7
    {{FULL, BOTH, FULL, 0}, {FULL, BOT, FULL, 0}},    // 8
    {{FULL, BOTH, FULL, 0}, {BOT, BOT, FULL, 0}},     // 9
    {{BOT, BOT, BOT, 0}, {' ', ' ', ' ', 0}},         // 10: dash, time unknown
};

static String wifiDot() { return String(WiFi.status() == WL_CONNECTED ? WIFI_DOT : ' '); }

// Screen 1: big HH:MM over both lines; the colon blinks once a second.
//   cols 0-2 hour tens, 4-6 hour units, 7 colon, 8-10 minute tens, 12-14 minute units
static void bigClock(String &l1, String &l2) {
  int d[4] = {10, 10, 10, 10};
  bool colon = true;
  if (timeSource != TimeSource::None) {
    time_t now = time(nullptr);
    tm lt;
    localtime_r(&now, &lt);
    d[0] = lt.tm_hour / 10;
    d[1] = lt.tm_hour % 10;
    d[2] = lt.tm_min / 10;
    d[3] = lt.tm_min % 10;
    colon = lt.tm_sec % 2 == 0;
  }
  for (int row = 0; row < 2; row++) {
    String &l = row ? l2 : l1;
    l = "";
    for (int i = 0; i < 4; i++) {
      if (i == 0 && d[0] == 0) l += F("   ");  // " 8:05", not "08:05"
      else l += BIG[d[i]][row];
      if (i == 1) l += colon ? (row ? COLON_DN : COLON_UP) : ' ';
      else if (i != 3) l += ' ';
    }
    l += ' ';  // no Wi-Fi dot here: it looked odd next to the big digits (Roman, 22.10.2026)
  }
}

// Screen 2: weather, a few seconds several times a minute.
//   23.5°C       53%
//   1008 hPa       •
static void weather(String &l1, String &l2) {
  char buf[24];
  snprintf(buf, sizeof(buf), "%.1f", env.t);
  l1 = String(buf) + DEG + 'C';
  if (!isnan(env.h)) {
    snprintf(buf, sizeof(buf), "%3.0f%%", env.h);
    while (l1.length() < 12) l1 += ' ';
    l1 += buf;
  }
  l2 = "";
  if (!isnan(env.p)) {
    snprintf(buf, sizeof(buf), "%.0f hPa", env.p);
    l2 = buf;
  }
  while (l2.length() < 15) l2 += ' ';
  l2 += wifiDot();
}

// Small text clock + one message line: first seconds after power-on and the setup mode.
static void startup(String &l1, String &l2, const String &msg) {
  char buf[16] = "--:--:--";
  if (timeSource != TimeSource::None) {
    time_t now = time(nullptr);
    tm lt;
    localtime_r(&now, &lt);
    strftime(buf, sizeof(buf), "%H:%M:%S", &lt);
  }
  l1 = buf;
  l2 = msg;
}

// Wi-Fi is reported in words only once after power-on ("WiFi OK" / "no network", 5 s);
// returns "" when that phase is over.
static String bootMessage() {
  static uint32_t firstConnectMs = 0, noNetMs = 0;
  static bool done = false;
  if (done) return "";
  uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED && !firstConnectMs) firstConnectMs = now;
  if (!firstConnectMs && !noNetMs && now > 30000) noNetMs = now;
  if (!firstConnectMs && !noNetMs) return F("WiFi...");
  if (firstConnectMs && now - firstConnectMs < 5000) return F("WiFi OK");
  if (!firstConnectMs && now - noNetMs < 5000) return F("WiFi: no network");
  done = true;
  return "";
}

static void compose(String &l1, String &l2) {
  if (st.apActive && !st.cfg.hasNetworks()) return startup(l1, l2, F("AP SVITLO-LOCAL"));  // first setup
  String msg = bootMessage();
  if (msg.length()) return startup(l1, l2, msg);
  // 12 s cycle: 9 s big clock, 3 s weather -> weather 5 times a minute
  if (env.ok && millis() % 12000 >= 9000) weather(l1, l2);
  else bigClock(l1, l2);
}

static void describe() {
  char buf[48];
  snprintf(buf, sizeof(buf), "LCD 1602 на 0x%02X (SDA=D6, SCL=D5)", addr);
  displayInfo = buf;
}

void displayBegin() {
  lcdBus();
  if (findLcd()) {
    lcdInit();
    describe();
    writeLine(0, F("Svitlo-Local"));
    writeLine(1, F("fw " FW_VERSION));
  } else {
    displayInfo = F("LCD не знайдено: перевірте SDA=D6, SCL=D5, VCC=VIN, GND");
  }
  Serial.println(displayInfo);
  mainBus();
  lastProbeMs = millis();
}

void displayLoop() {
  static uint32_t lastMs = 0;
  uint32_t now = millis();
  if (now - lastMs < 250) return;  // 4 updates a second is plenty for a clock with seconds
  lastMs = now;

  if (!addr || !busOk) {
    if (now - lastProbeMs < 10000) return;  // LCD missing or fell off: look again every 10 s
    lastProbeMs = now;
    lcdBus();
    if (findLcd()) {
      lcdInit();
      describe();
    }
    mainBus();
    if (!addr) return;
  }

  String l1, l2;
  compose(l1, l2);
  lcdBus();
  writeLine(0, l1);
  writeLine(1, l2);
  mainBus();
  if (!busOk) {
    displayInfo = F("LCD перестав відповідати, шукаю знову");
    addr = 0;
  }
}
