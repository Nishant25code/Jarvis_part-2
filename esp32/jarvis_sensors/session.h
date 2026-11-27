#pragma once
#include <Arduino.h>

// One signed conversation with Jarvis (one for USB, one for Wi-Fi).
struct Session {
  Print *out = nullptr;
  bool ready = false;     // session key agreed
  bool authed = false;    // the other side has sent at least one correctly signed line
  uint8_t key[32];
  uint32_t rxSeq = 0, txSeq = 0;
  uint32_t opened = 0, lastValid = 0;
  String line;
  void reset() {
    ready = authed = false;
    rxSeq = txSeq = 0;
    line = "";
    memset(key, 0, sizeof key);
  }
};
