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

static String line1() {
  char buf[24];
  String s;
  if (timeSource == TimeSource::None) {
    s = F("--:--:--");
  } else {
    time_t now = time(nullptr);
    tm lt;
    localtime_r(&now, &lt);
    strftime(buf, sizeof(buf), "%H:%M:%S", &lt);
    s = buf;
  }
  if (env.ok) {
    snprintf(buf, sizeof(buf), " %6.1f", env.t);
    s += buf;
    s += (char)0xDF;  // degree sign in the LCD's built-in font
    s += 'C';
  }
  return s;
}

static String line2() {
  char buf[24];
  bool statusScreen = !env.ok || (millis() / 5000) % 2;
  if (!statusScreen) {
    String s;
    if (!isnan(env.h)) {
      snprintf(buf, sizeof(buf), "%3.0f%%", env.h);
      s = buf;
    } else {
      s = F(" --%");
    }
    if (!isnan(env.p)) {
      snprintf(buf, sizeof(buf), "  %4.0fhPa", env.p);
      s += buf;
    }
    return s;
  }
  if (WiFi.status() != WL_CONNECTED) return st.apActive ? F("AP SVITLO-LOCAL") : F("WiFi: no network");
  const char *ping = !st.lastAttemptMs ? "PING .." : st.failsInRow ? "PING ERR" : "PING OK";
  snprintf(buf, sizeof(buf), "WiFi %d %s", WiFi.RSSI(), ping);
  return buf;
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

  String l1 = line1(), l2 = line2();
  lcdBus();
  writeLine(0, l1);
  writeLine(1, l2);
  mainBus();
  if (!busOk) {
    displayInfo = F("LCD перестав відповідати, шукаю знову");
    addr = 0;
  }
}
