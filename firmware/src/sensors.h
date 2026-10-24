#pragma once
#include <Arduino.h>

// Optional modules on the I2C bus (SDA = D2/GPIO4, SCL = D1/GPIO5):
//   BME280 (or BMP280) at 0x76/0x77 - temperature, humidity, pressure
//   DS3231 at 0x68                  - clock that keeps running without power and internet
// Every module may be missing: the board then simply works without it.

struct Env {
  String sensor;          // "BME280", "BMP280" or "" (not found)
  bool ok = false;        // last reading is valid
  float t = NAN;          // °C
  float h = NAN;          // %, NAN on BMP280
  float p = NAN;          // hPa
  uint32_t readMs = 0;
  uint32_t bad = 0;       // readings thrown away since boot (failed or implausible)
};

extern Env env;

// Clock: system time comes from DS3231 at boot, NTP corrects both when internet is there.
enum class TimeSource { None, Rtc, Ntp };
extern TimeSource timeSource;
extern bool rtcFound;
extern bool rtcLostPower;  // DS3231 reported that its battery ran out (time was not kept)

void sensorsBegin();  // I2C, DS3231 -> system time, BME280
void sensorsLoop();   // reads the sensor every 30 s, writes NTP time to DS3231
String i2cScan();     // addresses found on the bus, for the status page
