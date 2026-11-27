// Jarvis sensor hub for an ESP32.
//
// Jarvis (on your Mac) talks to this board over the USB cable, or over your
// home Wi-Fi if you fill in secrets.h. Either way every message is signed:
//
//   1. Jarvis sends    "hello <client nonce>"
//   2. The board says  "hello <board nonce> jarvis-sensors"
//   3. Both derive a session key from the shared secret and the two nonces, and
//      every line after that is "<seq> <mac> <payload>", where mac is HMAC-SHA256
//      over the direction, the sequence number and the payload. A wrong key, a
//      replayed line or a tampered line is dropped (and the Wi-Fi client cut off).
//
// So nobody else on your network can read the sensors, switch the relay, or
// fake a "touch" that wakes Jarvis. No cloud, no extra libraries.
//
// Commands (payload "<id> <command> [args]"): ping, info, read, relay 0|1,
// led R G B, beep MS [COUNT]. Events the board sends on its own: touch, door, flame.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include "mbedtls/md.h"
#include "config.h"
#include "secrets.h"
#include "session.h"

#define FW_VERSION "1.0"
#define MAX_LINE 300

// ── Signing ────────────────────────────────────────────────────────────────

static uint8_t masterKey[32];
static bool keyOk = false;

static void hmac256(const uint8_t *key, size_t klen, const String &msg, uint8_t out[32]) {
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, klen,
                  (const uint8_t *)msg.c_str(), msg.length(), out);
}

static String toHex(const uint8_t *b, size_t n) {
  static const char *d = "0123456789abcdef";
  String s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; i++) {
    s += d[b[i] >> 4];
    s += d[b[i] & 15];
  }
  return s;
}

static int nib(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static bool fromHex(const char *s, uint8_t *out, size_t n) {
  if (strlen(s) != n * 2) return false;
  for (size_t i = 0; i < n; i++) {
    int a = nib(s[2 * i]), b = nib(s[2 * i + 1]);
    if (a < 0 || b < 0) return false;
    out[i] = (a << 4) | b;
  }
  return true;
}

static bool sameText(const String &a, const String &b) {   // constant time
  if (a.length() != b.length()) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < a.length(); i++) diff |= a[i] ^ b[i];
  return diff == 0;
}


static Session usb, net;
static NetworkServer server(TCP_PORT);
static NetworkClient netClient;

static String macFor(Session &s, char dir, uint32_t seq, const String &payload) {
  uint8_t m[32];
  hmac256(s.key, sizeof s.key, String(dir) + "|" + String(seq) + "|" + payload, m);
  return toHex(m, 16);
}

static void sendFrame(Session &s, const String &payload) {
  if (!s.ready || !s.out) return;
  s.txSeq++;
  s.out->print(String(s.txSeq) + " " + macFor(s, 'd', s.txSeq, payload) + " " + payload + "\n");
}

static void sendEvent(const String &payload) {
  if (usb.ready && usb.authed) sendFrame(usb, payload);
  if (net.ready && net.authed && netClient.connected()) sendFrame(net, payload);
}

// ── Hardware ───────────────────────────────────────────────────────────────

static bool relayOn = false;
static uint8_t ledRGB[3] = {0, 0, 0};
static int beepLeft = 0;           // half-cycles still to play
static uint32_t beepMs = 0, beepNext = 0;
static bool beepState = false;

static float dhtTemp = NAN, dhtHum = NAN;
static uint32_t dhtAt = 0;

static void setRelay(bool on) {
  relayOn = on;
#if USE_RELAY
  digitalWrite(RELAY_PIN, (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW);
#endif
}

static void setLed(uint8_t r, uint8_t g, uint8_t b) {
  ledRGB[0] = r; ledRGB[1] = g; ledRGB[2] = b;
#if USE_LED && LED_ONBOARD
  rgbLedWrite(ONBOARD_LED_PIN, r / 4, g / 4, b / 4);   // the onboard LED is very bright: quarter power
#elif USE_LED
  const uint8_t pins[3] = {LED_R_PIN, LED_G_PIN, LED_B_PIN};
  for (int i = 0; i < 3; i++) ledcWrite(pins[i], LED_COMMON_ANODE ? 255 - ledRGB[i] : ledRGB[i]);
#endif
}

static void setBuzzer(bool on) {
#if USE_BUZZER
  digitalWrite(BUZZER_PIN, on ? HIGH : LOW);
#endif
  beepState = on;
}

#if USE_DHT11
// DHT11 without a library: 18 ms start pulse, then 40 bits whose HIGH length says 0 (~27 µs) or 1 (~70 µs).
static portMUX_TYPE dhtMux = portMUX_INITIALIZER_UNLOCKED;

static int32_t waitWhile(int level, uint32_t timeout) {
  uint32_t start = micros();
  while (digitalRead(DHT_PIN) == level) {
    if (micros() - start > timeout) return -1;
  }
  return micros() - start;
}

static bool readDHT11(float &t, float &h) {
  uint8_t d[5] = {0};
  pinMode(DHT_PIN, OUTPUT);
  digitalWrite(DHT_PIN, LOW);
  delay(20);
  digitalWrite(DHT_PIN, HIGH);
  delayMicroseconds(30);
  pinMode(DHT_PIN, INPUT_PULLUP);
  bool ok = true;
  portENTER_CRITICAL(&dhtMux);
  if (waitWhile(HIGH, 100) < 0 || waitWhile(LOW, 100) < 0 || waitWhile(HIGH, 100) < 0) ok = false;
  for (int i = 0; ok && i < 40; i++) {
    if (waitWhile(LOW, 80) < 0) { ok = false; break; }
    int32_t high = waitWhile(HIGH, 100);
    if (high < 0) { ok = false; break; }
    d[i / 8] = (d[i / 8] << 1) | (high > 40 ? 1 : 0);
  }
  portEXIT_CRITICAL(&dhtMux);
  if (!ok || (uint8_t)(d[0] + d[1] + d[2] + d[3]) != d[4]) return false;
  h = d[0] + d[1] * 0.1f;
  t = d[2] + (d[3] & 0x7F) * 0.1f;
  if (d[3] & 0x80) t = -t;
  return h <= 100;
}
#endif

static void refreshDHT(bool force) {
#if USE_DHT11
  if (!force && dhtAt && millis() - dhtAt < 2500) return;   // the DHT11 needs ~1 s between reads
  float t, h;
  for (int tries = 0; tries < 3; tries++) {
    if (readDHT11(t, h)) {
      dhtTemp = t; dhtHum = h; dhtAt = millis();
      return;
    }
    delay(50);
  }
  if (millis() - dhtAt > 30000) { dhtTemp = NAN; dhtHum = NAN; }   // stale: report nothing rather than old values
#endif
}

// -1 when nothing is connected: a floating pin just follows whichever pull resistor is on,
// while a real sensor module holds its own voltage.
static int analogPercent(int pin, bool invert) {
  pinMode(pin, INPUT_PULLUP);
  delay(3);
  int high = analogRead(pin);
  pinMode(pin, INPUT_PULLDOWN);
  delay(3);
  int low = analogRead(pin);
  pinMode(pin, INPUT);
  delay(3);
  if (high - low > 2400) return -1;
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogRead(pin);
  int pct = (int)((sum / 8) * 100 / 4095);
  return invert ? 100 - pct : pct;
}

static String pct(int v) { return v < 0 ? String("null") : String(v); }

static float distanceCm() {
#if USE_DISTANCE
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(3);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long us = pulseIn(ECHO_PIN, HIGH, 25000);   // ~4 m max
  if (us == 0) return NAN;
  return us / 58.0f;
#else
  return NAN;
#endif
}

static String num(float v, int digits = 1) { return isnan(v) ? String("null") : String(v, digits); }

static String wifiState() {
  if (strlen(WIFI_SSID) == 0) return "off";
  return WiFi.status() == WL_CONNECTED ? "connected" : "connecting";
}

static String readingsJson() {
  String j = "\"uptime_s\":" + String(millis() / 1000);
#if USE_DHT11
  refreshDHT(false);
  j += ",\"temp_c\":" + num(dhtTemp) + ",\"humidity\":" + num(dhtHum, 0);
#endif
#if USE_LIGHT
  j += ",\"light\":" + pct(analogPercent(LIGHT_PIN, LIGHT_INVERT));
#endif
#if USE_SOIL
  j += ",\"soil\":" + pct(analogPercent(SOIL_PIN, SOIL_INVERT));
#endif
#if USE_DISTANCE
  j += ",\"distance_cm\":" + num(distanceCm());
#endif
#if USE_TOUCH
  j += String(",\"touch\":") + (digitalRead(TOUCH_PIN) ? "true" : "false");
#endif
#if USE_DOOR
  j += String(",\"door_open\":") + (digitalRead(DOOR_PIN) ? "true" : "false");
#endif
#if USE_FLAME
  j += String(",\"flame\":") + ((digitalRead(FLAME_PIN) == (FLAME_ACTIVE_LOW ? LOW : HIGH)) ? "true" : "false");
#endif
#if USE_RELAY
  j += String(",\"relay\":") + (relayOn ? "true" : "false");
#endif
  return j;
}

static String infoJson() {
  String parts = "";
  auto add = [&](bool on, const char *name) {
    if (!on) return;
    if (parts.length()) parts += ",";
    parts += "\"" + String(name) + "\"";
  };
  add(USE_DHT11, "temperature"); add(USE_DHT11, "humidity"); add(USE_LIGHT, "light");
  add(USE_DISTANCE, "distance"); add(USE_TOUCH, "touch"); add(USE_SOIL, "soil");
  add(USE_DOOR, "door"); add(USE_FLAME, "flame"); add(USE_LED, "led");
  add(USE_BUZZER, "buzzer"); add(USE_RELAY, "relay");
  String j = "\"fw\":\"" FW_VERSION "\",\"name\":\"" DEVICE_NAME "\",\"parts\":[" + parts + "]";
  j += ",\"wifi\":\"" + wifiState() + "\"";
  if (WiFi.status() == WL_CONNECTED) j += ",\"ip\":\"" + WiFi.localIP().toString() + "\",\"rssi\":" + String(WiFi.RSSI());
  return j;
}

// ── Commands ───────────────────────────────────────────────────────────────

static String handle(const String &payload) {
  char buf[MAX_LINE];
  payload.toCharArray(buf, sizeof buf);
  char *save = nullptr;
  char *idTok = strtok_r(buf, " ", &save);
  char *cmd = strtok_r(nullptr, " ", &save);
  long id = idTok ? strtol(idTok, nullptr, 10) : 0;
  String head = "{\"id\":" + String(id) + ",";
  if (!cmd) return head + "\"ok\":false,\"error\":\"empty\"}";
  long a[3] = {0, 0, 0};
  int n = 0;
  for (char *t; n < 3 && (t = strtok_r(nullptr, " ", &save));) a[n++] = strtol(t, nullptr, 10);

  if (!strcmp(cmd, "ping")) return head + "\"ok\":true}";
  if (!strcmp(cmd, "info")) return head + "\"ok\":true," + infoJson() + "}";
  if (!strcmp(cmd, "read")) return head + "\"ok\":true," + readingsJson() + "}";
  if (!strcmp(cmd, "relay")) {
    if (!USE_RELAY) return head + "\"ok\":false,\"error\":\"no relay connected\"}";
    if (n < 1) return head + "\"ok\":false,\"error\":\"relay needs 0 or 1\"}";
    setRelay(a[0] != 0);
    return head + "\"ok\":true,\"relay\":" + (relayOn ? "true" : "false") + "}";
  }
  if (!strcmp(cmd, "led")) {
    if (!USE_LED) return head + "\"ok\":false,\"error\":\"no LED connected\"}";
    if (n < 3) return head + "\"ok\":false,\"error\":\"led needs R G B\"}";
    setLed(constrain(a[0], 0, 255), constrain(a[1], 0, 255), constrain(a[2], 0, 255));
    return head + "\"ok\":true}";
  }
  if (!strcmp(cmd, "beep")) {
    if (!USE_BUZZER) return head + "\"ok\":false,\"error\":\"no buzzer connected\"}";
    beepMs = constrain(n > 0 ? a[0] : 150, 20, 2000);
    beepLeft = 2 * constrain(n > 1 ? a[1] : 1, 1, 5);
    beepNext = millis();
    return head + "\"ok\":true}";
  }
  return head + "\"ok\":false,\"error\":\"unknown command\"}";
}

// Returns false if the line was forged or out of order (the caller then drops the link).
static bool onLine(Session &s, const String &line) {
  if (line.startsWith("hello ")) {
    String cnonce = line.substring(6);
    cnonce.trim();
    uint8_t tmp[16];
    if (!keyOk || !fromHex(cnonce.c_str(), tmp, 16)) return false;
    uint8_t dn[16];
    esp_fill_random(dn, sizeof dn);
    String dnonce = toHex(dn, 16);
    s.reset();
    hmac256(masterKey, sizeof masterKey, "jarvis-session|" + cnonce + "|" + dnonce, s.key);
    s.ready = true;
    s.out->print("hello " + dnonce + " " DEVICE_NAME "\n");
    return true;
  }
  if (!s.ready) return false;
  int sp1 = line.indexOf(' '), sp2 = sp1 < 0 ? -1 : line.indexOf(' ', sp1 + 1);
  if (sp1 < 1 || sp2 < 0) return false;
  uint32_t seq = strtoul(line.substring(0, sp1).c_str(), nullptr, 10);
  String mac = line.substring(sp1 + 1, sp2), payload = line.substring(sp2 + 1);
  if (seq != s.rxSeq + 1 || !sameText(mac, macFor(s, 'c', seq, payload))) return false;
  s.rxSeq = seq;
  s.authed = true;
  s.lastValid = millis();
  sendFrame(s, handle(payload));
  return true;
}

static void pump(Session &s, Stream &in, bool isNet) {
  while (in.available()) {
    char c = in.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (s.line.length() >= MAX_LINE) {          // garbage or an attack: start over
        s.line = "";
        if (isNet) { netClient.stop(); net.reset(); return; }
      } else {
        s.line += c;
      }
      continue;
    }
    String line = s.line;
    s.line = "";
    line.trim();
    if (!line.length()) continue;
    if (!onLine(s, line)) {
      if (isNet) { netClient.stop(); net.reset(); return; }
      s.reset();                                  // USB: just forget the session
    }
  }
}

// ── Setup / loop ───────────────────────────────────────────────────────────

static bool netStarted = false;

void setup() {
  Serial.begin(115200);
  usb.out = &Serial;
  net.out = &netClient;
  keyOk = fromHex(JARVIS_KEY, masterKey, 32);

#if USE_RELAY
  pinMode(RELAY_PIN, OUTPUT);
  setRelay(false);
#endif
#if USE_BUZZER
  pinMode(BUZZER_PIN, OUTPUT);
  setBuzzer(false);
#endif
#if USE_LED && !LED_ONBOARD
  ledcAttach(LED_R_PIN, 5000, 8);
  ledcAttach(LED_G_PIN, 5000, 8);
  ledcAttach(LED_B_PIN, 5000, 8);
#endif
#if USE_LED
  setLed(0, 0, 0);
#endif
#if USE_DISTANCE
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
#endif
#if USE_TOUCH
  pinMode(TOUCH_PIN, INPUT_PULLDOWN);   // an unwired pin reads "not touched", never random taps
#endif
#if USE_DOOR
  pinMode(DOOR_PIN, INPUT_PULLUP);
#endif
#if USE_FLAME
  pinMode(FLAME_PIN, INPUT);
#endif
  analogReadResolution(12);

  if (strlen(WIFI_SSID) > 0) {
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(DEVICE_NAME);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  Serial.println(keyOk ? "jarvis-sensors ready" : "jarvis-sensors: no key, run `python3 sensors.py setup` and flash again");
}

void loop() {
  uint32_t now = millis();

  // Wi-Fi: start the server and mDNS name once connected.
  if (strlen(WIFI_SSID) > 0 && WiFi.status() == WL_CONNECTED && !netStarted) {
    MDNS.begin(DEVICE_NAME);
    MDNS.addService("jarvis", "tcp", TCP_PORT);
    server.begin();
    server.setNoDelay(true);
    netStarted = true;
  }
  if (netStarted) {
    NetworkClient incoming = server.accept();
    if (incoming) {
      bool busy = netClient.connected() && net.authed && now - net.lastValid < 30000;
      if (busy) {
        incoming.stop();                          // Jarvis is connected and alive: nobody else
      } else {
        netClient.stop();
        netClient = incoming;
        net.reset();
        net.opened = now;
        netClient.setNoDelay(true);
      }
    }
    if (netClient.connected() && !net.authed && now - net.opened > 5000) {
      netClient.stop();                           // must prove the key within 5 s
      net.reset();
    }
    if (netClient.connected()) pump(net, netClient, true);
  }
  pump(usb, Serial, false);

  // Beeps without blocking the loop.
  if (beepLeft > 0 && (int32_t)(now - beepNext) >= 0) {
    setBuzzer(!beepState);
    beepLeft--;
    beepNext = now + beepMs;
    if (beepLeft == 0) setBuzzer(false);
  }

#if USE_TOUCH
  static bool touchWas = false;
  static uint32_t touchAt = 0;
  bool touch = digitalRead(TOUCH_PIN);
  if (touch != touchWas && now - touchAt > 60) {
    touchWas = touch;
    touchAt = now;
    if (touch) sendEvent("{\"event\":\"touch\"}");
  }
#endif
#if USE_DOOR
  static int doorWas = -1;
  static uint32_t doorAt = 0;
  int door = digitalRead(DOOR_PIN);
  if (door != doorWas && now - doorAt > 200) {
    if (doorWas >= 0) sendEvent(String("{\"event\":\"door\",\"open\":") + (door ? "true" : "false") + "}");
    doorWas = door;
    doorAt = now;
  }
#endif
#if USE_FLAME
  static uint32_t flameAt = 0;
  bool flame = digitalRead(FLAME_PIN) == (FLAME_ACTIVE_LOW ? LOW : HIGH);
  if (flame && (flameAt == 0 || now - flameAt > 30000)) {
    flameAt = now;
    sendEvent("{\"event\":\"flame\"}");
  }
#endif
  delay(2);
}
