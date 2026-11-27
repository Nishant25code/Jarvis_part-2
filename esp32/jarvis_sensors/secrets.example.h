#pragma once
// `python3 sensors.py setup` creates secrets.h from this, with a fresh random key.
// secrets.h stays on this Mac (it's in .gitignore and readable only by you).

#define JARVIS_KEY ""   // 64 hex characters, shared with ~/.jarvis/sensors.json

// Optional Wi-Fi. Leave both empty to use the USB cable only.
// To use Wi-Fi, type your network's name and password here yourself, then flash again.
#define WIFI_SSID ""
#define WIFI_PASS ""
