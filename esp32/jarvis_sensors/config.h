#pragma once
// Which parts are plugged in, and where. Set a part to 0 if it isn't connected,
// so Jarvis never reports readings from an empty pin. See esp32/README.md for wiring.

// ── What's connected ───────────────────────────────────────────────────────
#define USE_DHT11      0    // temperature + humidity
#define USE_LIGHT      1    // photoresistor (LDR) module, AO pin
#define LIGHT_INVERT   1    // most LDR modules give a LOWER voltage in brighter light
#define USE_DISTANCE   0    // HC-SR04 ultrasonic: is someone at the desk? (echo is 5 V: see README)
#define USE_TOUCH      1    // TTP223 touch module: tap to wake Jarvis
#define USE_SOIL       0    // soil moisture sensor, AO pin
#define SOIL_INVERT    1    // wetter soil = lower voltage on these probes
#define USE_DOOR       0    // reed switch: magnet near = closed
#define USE_FLAME      0    // flame sensor DO pin
#define FLAME_ACTIVE_LOW 1  // most flame modules pull DO low when they see a flame; flip if reversed
#define USE_LED        1    // status light (blue listening, purple thinking, green speaking)
#define LED_COMMON_ANODE 0  // external KY-016 module is common cathode (0)
#define USE_BUZZER     1    // ACTIVE buzzer module
#define USE_RELAY      1    // 1-channel relay module
#define RELAY_ACTIVE_LOW 0  // flip to 1 if the relay clicks ON when Jarvis says off

// ── Pins ───────────────────────────────────────────────────────────────────
#if CONFIG_IDF_TARGET_ESP32S3
// ESP32-S3 DevKit (yours). GPIO 26–37 belong to flash/PSRAM, 19/20 to USB, 43/44 to the USB-serial chip.
#define LED_ONBOARD    1    // 1 = the board's own RGB LED (GPIO48); 0 = an external RGB module on the pins below
#define ONBOARD_LED_PIN 48  // some S3 boards use 38: change this if the LED stays dark
#define DHT_PIN        4
#define LIGHT_PIN      1    // ADC1 (GPIO 1–10): ADC2 stops working when Wi-Fi is on
#define SOIL_PIN       2
#define TRIG_PIN       5
#define ECHO_PIN       6
#define TOUCH_PIN      7
#define LED_R_PIN      10
#define LED_G_PIN      11
#define LED_B_PIN      12
#define BUZZER_PIN     13
#define RELAY_PIN      14
#define DOOR_PIN       15
#define FLAME_PIN      16
#else
// Classic ESP32 DevKit (ESP32-WROOM-32).
#define LED_ONBOARD    0
#define ONBOARD_LED_PIN 0
#define DHT_PIN        4
#define LIGHT_PIN      34   // ADC1 only (32–39)
#define SOIL_PIN       35
#define TRIG_PIN       13
#define ECHO_PIN       18
#define TOUCH_PIN      27
#define LED_R_PIN      25
#define LED_G_PIN      26
#define LED_B_PIN      33
#define BUZZER_PIN     23
#define RELAY_PIN      19
#define DOOR_PIN       32
#define FLAME_PIN      39
#endif

// ── Network (only used when WIFI_SSID in secrets.h is filled in) ───────────
#define DEVICE_NAME    "jarvis-sensors"   // reachable as jarvis-sensors.local
#define TCP_PORT       7878
