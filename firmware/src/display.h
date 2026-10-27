#pragma once
#include <Arduino.h>

// LCD 1602 with an I2C backpack (PCF8574), on its OWN pair of pins: SDA = D6 (GPIO12), SCL = D5 (GPIO14),
// powered from VIN (5 V). It is kept off the sensor bus (D1/D2) because the backpack may pull the
// lines up to 5 V, which a 3.3 V BME280 does not survive.
// Big HH:MM over both lines; every 12 s for 3 s: temperature, humidity, pressure.
// Weather screen: dot bottom-right = Wi-Fi connected. Wi-Fi in words only for 5 s after power-on.
bool displayPresent();  // an LCD answered on the display bus
extern String displayInfo;  // what was found, for the status page
extern String displayNote;  // shown instead of everything else while not empty (factory reset countdown)
void displayBegin();
void displayLoop();
