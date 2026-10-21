#pragma once
#include <Arduino.h>

// LCD 1602 with an I2C backpack (PCF8574), on its OWN pair of pins: SDA = D6 (GPIO12), SCL = D5 (GPIO14),
// powered from VIN (5 V). It is kept off the sensor bus (D1/D2) because the backpack may pull the
// lines up to 5 V, which a 3.3 V BME280 does not survive.
//   line 1: 22:05:31  23.7°C
//   line 2: 54%  1012hPa   <-> every 5 s ->   WiFi -60 PING OK
extern String displayInfo;  // what was found, for the status page
void displayBegin();
void displayLoop();
