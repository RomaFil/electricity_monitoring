#pragma once
#include <WiFiClientSecureBearSSL.h>

// Root certificates for HTTPS, parsed once and shared by all modules.
//
// Every BearSSL connection needs one contiguous ~16.7 KB receive buffer (none of our servers accepts a
// smaller TLS fragment length), and the board has only ~32 KB free. When each module parsed its own
// list on first use, the lists landed in the middle of the heap; after the first forecast no 16.7 KB
// block was left, every HTTPS request failed with "connection failed", and soon the Wi-Fi stack itself
// crashed on an allocation (Exception 29 in ieee80211_setup_ratetable, 24.10.2026). Parsed at the very
// start of setup() they sit at the bottom of the heap and leave the rest in one piece.
void trustBegin();
BearSSL::X509List *trustSectigo();      // hc-ping.com, healthchecks.io
