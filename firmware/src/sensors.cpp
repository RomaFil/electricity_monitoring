#include "sensors.h"

#include <Adafruit_BME280.h>
#include <Adafruit_BMP280.h>
#include <RTClib.h>
#include <Wire.h>
#include <coredecls.h>  // settimeofday_cb
#include <sys/time.h>

#include "state.h"

Env env;
TimeSource timeSource = TimeSource::None;
bool rtcFound = false;
bool rtcLostPower = false;

static RTC_DS3231 rtc;
static Adafruit_BME280 bme;
static Adafruit_BMP280 bmp;
static uint8_t bmeAddr = 0;
static volatile bool ntpJustSynced = false;
static uint32_t lastRtcWriteMs = 0;
static uint32_t lastReadMs = 0;
static uint32_t lastProbeMs = 0;

static const uint32_t READ_EVERY_MS = 30000;
static const uint32_t PROBE_EVERY_MS = 60000;      // look for a sensor that was missing at boot
static const uint32_t RTC_WRITE_EVERY_MS = 3600000; // NTP -> DS3231 at most once an hour

static bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

static uint8_t chipId(uint8_t addr) {
  Wire.beginTransmission(addr);
  Wire.write(0xD0);  // BME280/BMP280 "id" register: 0x60 = BME280, 0x58 = BMP280
  if (Wire.endTransmission() != 0) return 0;
  if (Wire.requestFrom(addr, (uint8_t)1) != 1) return 0;
  return Wire.read();
}

static void probeSensor() {
  env.sensor = "";
  for (uint8_t a : {0x76, 0x77}) {
    uint8_t id = chipId(a);
    if (id == 0x60 && bme.begin(a, &Wire)) {
      // Forced mode: one measurement on request, the chip sleeps between them and does not heat itself.
      bme.setSampling(Adafruit_BME280::MODE_FORCED, Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::SAMPLING_X1,
                      Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::FILTER_OFF);
      env.sensor = "BME280";
      // The library applies the offset inside its maths, so humidity and pressure use the corrected temperature
      bme.setTemperatureCompensation(st.cfg.tempOffset);
    } else if (id == 0x58 && bmp.begin(a, 0x58)) {
      bmp.setSampling(Adafruit_BMP280::MODE_FORCED, Adafruit_BMP280::SAMPLING_X1, Adafruit_BMP280::SAMPLING_X1,
                      Adafruit_BMP280::FILTER_OFF);
      env.sensor = "BMP280";
    }
    if (env.sensor.length()) {
      bmeAddr = a;
      Serial.printf("sensor %s at 0x%02X\n", env.sensor.c_str(), a);
      return;
    }
  }
}

static void readSensor() {
  float t = NAN, h = NAN, p = NAN;
  if (env.sensor == "BME280") {
    if (bme.takeForcedMeasurement()) {
      t = bme.readTemperature();
      h = constrain(bme.readHumidity() + st.cfg.humOffset, 0.0f, 100.0f);
      p = bme.readPressure() / 100.0f;
    }
  } else if (env.sensor == "BMP280") {
    if (bmp.takeForcedMeasurement()) {
      t = bmp.readTemperature() + st.cfg.tempOffset;
      p = bmp.readPressure() / 100.0f;
    }
  }
  // A sensor that fell off the bus returns nonsense; treat anything out of range as "no reading".
  env.ok = !isnan(t) && t > -40 && t < 85;
  env.t = env.ok ? t : NAN;
  env.h = env.ok && !isnan(h) && h >= 0 && h <= 100 ? h : NAN;
  env.p = env.ok && !isnan(p) && p > 300 && p < 1100 ? p : NAN;
  env.readMs = millis();
  if (!env.ok) {
    Serial.println(F("sensor reading failed, will probe again"));
    env.sensor = "";
  }
}

void sensorsBegin() {
  Wire.begin(4, 5);  // SDA = D2, SCL = D1
  Wire.setClock(100000);

  settimeofday_cb([](bool fromSntp) {
    if (fromSntp) {
      timeSource = TimeSource::Ntp;
      ntpJustSynced = true;
    }
  });

  rtcFound = present(0x68) && rtc.begin(&Wire);
  if (rtcFound) {
    rtcLostPower = rtc.lostPower();
    DateTime n = rtc.now();  // DS3231 keeps UTC
    if (!rtcLostPower && n.year() >= 2025 && n.year() < 2100) {
      timeval tv = {(time_t)n.unixtime(), 0};
      settimeofday(&tv, nullptr);
      timeSource = TimeSource::Rtc;
      Serial.printf("time from DS3231: %04d-%02d-%02d %02d:%02d UTC\n", n.year(), n.month(), n.day(), n.hour(), n.minute());
    } else {
      Serial.println(F("DS3231 has no valid time yet (battery was out or never set)"));
    }
  } else {
    Serial.println(F("DS3231 not found"));
  }

  probeSensor();
  if (env.sensor.length()) readSensor();
  lastReadMs = lastProbeMs = millis();
  Serial.printf("I2C: %s\n", i2cScan().c_str());
}

void sensorsLoop() {
  uint32_t now = millis();
  if (ntpJustSynced && rtcFound && (!lastRtcWriteMs || now - lastRtcWriteMs > RTC_WRITE_EVERY_MS || rtcLostPower)) {
    rtc.adjust(DateTime((uint32_t)time(nullptr)));  // also clears the "lost power" flag
    rtcLostPower = false;
    lastRtcWriteMs = now;
    Serial.println(F("DS3231 set from NTP"));
  }
  ntpJustSynced = false;

  if (!env.sensor.length()) {
    if (now - lastProbeMs > PROBE_EVERY_MS) {
      lastProbeMs = now;
      probeSensor();
    }
    if (!env.sensor.length()) return;
    lastReadMs = 0;  // just found: read at once
  }
  if (!lastReadMs || now - lastReadMs > READ_EVERY_MS) {
    lastReadMs = now;
    readSensor();
  }
}

String i2cScan() {
  String s;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    if (present(a)) {
      char buf[8];
      snprintf(buf, sizeof(buf), "0x%02X ", a);
      s += buf;
    }
  }
  return s.length() ? s : String(F("нічого"));
}
