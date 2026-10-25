#include "forecast.h"

#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <memory>

#include "state.h"

Forecast fc;

static const uint32_t EVERY_MS = 30UL * 60 * 1000;  // forecasts change slowly
static const uint32_t RETRY_MS = 5UL * 60 * 1000;
static const uint32_t FIRST_DELAY_MS = 20000;       // let the first heartbeat go first after boot

// WMO weather code (what Open-Meteo returns) -> one of our 8 icons
static Sky sky(int code, bool day) {
  if (code <= 1) return day ? Sky::Clear : Sky::ClearNight;
  if (code == 2) return Sky::Partly;
  if (code == 3) return Sky::Cloudy;
  if (code == 45 || code == 48) return Sky::Fog;
  if ((code >= 71 && code <= 77) || code == 85 || code == 86) return Sky::Snow;
  if (code >= 95) return Sky::Storm;
  if (code >= 51) return Sky::Rain;  // drizzle, rain, showers
  return Sky::Cloudy;
}

static bool fetch() {
  const Config &c = st.cfg;
  char url[320];
  snprintf(url, sizeof(url),
           "http://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
           "&current=temperature_2m,weather_code,is_day"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Europe%%2FKyiv&forecast_days=2",
           c.fcLat, c.fcLon);

  // Plain HTTP on purpose: a weather forecast is no secret, and HTTPS to Open-Meteo was the memory peak
  // of the board (Let's Encrypt chain with an RSA-4096 root) - every 30 minutes, during that request, the
  // Wi-Fi stack ran out of memory and crashed (Exception 29 in ieee80211_setup_ratetable, night 25.10.2026).
  std::unique_ptr<WiFiClient> tcp(new WiFiClient());

  HTTPClient http;
  http.useHTTP10(true);  // no chunked encoding, so the JSON can be parsed straight from the stream
  if (!http.begin(*tcp, url)) {
    fc.error = F("bad URL");
    return false;
  }
  http.setTimeout(15000);
  int code = http.GET();
  if (code != 200) {
    fc.error = code < 0 ? http.errorToString(code) : String(F("HTTP ")) + code;
    http.end();
    return false;
  }

  // Keep only the fields we use: the whole answer would not fit comfortably in RAM.
  JsonDocument filter;
  filter["current"]["temperature_2m"] = true;
  filter["current"]["weather_code"] = true;
  filter["current"]["is_day"] = true;
  filter["daily"]["weather_code"] = true;
  filter["daily"]["temperature_2m_max"] = true;
  filter["daily"]["temperature_2m_min"] = true;
  filter["daily"]["precipitation_probability_max"] = true;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();
  if (err) {
    fc.error = String(F("JSON: ")) + err.c_str();
    return false;
  }

  JsonObject cur = doc["current"];
  JsonObject daily = doc["daily"];
  if (cur["temperature_2m"].isNull() || daily["temperature_2m_max"][1].isNull()) {
    fc.error = F("no data in answer");
    return false;
  }
  fc.nowT = cur["temperature_2m"];
  fc.nowSky = sky(cur["weather_code"] | 3, (cur["is_day"] | 1) == 1);
  fc.tmrMax = daily["temperature_2m_max"][1];
  fc.tmrMin = daily["temperature_2m_min"][1];
  fc.tmrRain = daily["precipitation_probability_max"][1] | -1;
  fc.tmrSky = sky(daily["weather_code"][1] | 3, true);
  fc.error = "";
  return true;
}

void forecastLoop() {
  static uint32_t nextMs = FIRST_DELAY_MS;
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED || (int32_t)(now - nextMs) < 0) return;
  if (!st.cfg.displayOn) return;  // monitoring mode: the forecast is only for the screen
  bool ok = fetch();
  if (ok) {
    fc.ok = true;
    fc.fetchedMs = now;
  } else if (fc.ok && now - fc.fetchedMs > 3 * EVERY_MS) {
    fc.ok = false;  // older than 1.5 h: better nothing than a stale forecast
  }
  nextMs = now + (ok ? EVERY_MS : RETRY_MS);
  Serial.printf("forecast %s %s\n", ok ? "ok" : "FAIL", fc.error.c_str());
}
