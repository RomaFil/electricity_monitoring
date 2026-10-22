#pragma once
#include <Arduino.h>

// Weather forecast from Open-Meteo (free, no key): now + tomorrow for the place in the settings.
// Fetched every 30 minutes over HTTPS; on any error the forecast screen is simply skipped.

enum class Sky : uint8_t { Clear, ClearNight, Partly, Cloudy, Fog, Rain, Snow, Storm };

struct Forecast {
  bool ok = false;
  uint32_t fetchedMs = 0;
  float nowT = NAN;
  Sky nowSky = Sky::Cloudy;
  float tmrMin = NAN, tmrMax = NAN;
  int tmrRain = -1;  // max chance of precipitation tomorrow, %
  Sky tmrSky = Sky::Cloudy;
  String error;
};

extern Forecast fc;

void forecastLoop();
