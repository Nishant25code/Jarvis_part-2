# Jarvis, part 2: the hardware

Jarvis is a voice assistant that runs on a Mac. This repository holds only the hardware part: an
**ESP32-S3 board with sensors** that gives Jarvis senses and switches in the room. The rest of the
assistant (speech, the language model, apps and so on) is not included.

## What it does

| Part on the ESP32 | What Jarvis does with it |
|---|---|
| **Touch sensor** (GPIO7) | A tap wakes Jarvis, like saying "Hey Jarvis", so the microphone can stay off |
| **Light sensor (LDR)** (GPIO1) | "Is it dark in here?" |
| **Relay** (GPIO14) | "Turn on the relay" switches a lamp or fan (low voltage only) |
| **Active buzzer** (GPIO13) | "Buzz three times", "Give me a long beep" |
| **Built-in RGB LED** (GPIO48) | Shows what Jarvis is doing (blue listening, purple thinking, green speaking), or any colour you ask for |
| DHT11, HC-SR04, soil, reed switch, flame | Supported by the firmware, switched off in `config.h` for this build |

## How it works

```
  Mac (Python)                                  ESP32-S3 (Arduino C++)
  Jarvis -> tools/sensors.py -> sensors.py <== USB serial or Wi-Fi (TCP 7878) ==> jarvis_sensors.ino
            (what the assistant  (connection,   every line signed (HMAC-SHA256)    (reads sensors, drives
             can ask for)         signing)                                          relay/LED/buzzer, sends taps)
```

1. **Firmware** (`esp32/jarvis_sensors/`): reads the sensors, drives the relay, LED and buzzer, and
   sends a `touch` event when the pad is tapped. It needs no external libraries: the DHT11 driver is
   hand-written, and signing uses mbedTLS from the ESP32 core.
2. **Host link** (`sensors.py`): finds the board over USB first, then Wi-Fi (`jarvis-sensors.local`).
   It does the handshake, signs every command and checks every reply. It reconnects by itself if the
   cable is pulled, and also has the `setup`, `flash`, `test` and `ports` commands.
3. **Assistant tools** (`tools/sensors.py`): the functions the language model can call:
   - `room_sensors`, `switch_relay`, `set_status_light`, `sensor_beep`, `sensor_hub_status`
   - `board_intent()`: turns everyday wording into an exact call. "Can you buzz three times?" becomes
     `sensor_beep(times=3)`, and "make the LED violet" becomes `set_status_light("purple")`, so the
     hardware does what was asked even when a small local model picks the wrong tool.

## Security

The relay switches real things, and a tap wakes the microphone. So the link is protected, even over Wi-Fi:

- **A shared 256-bit key** is created once on the Mac (`~/.jarvis/sensors.json`, permissions 0600)
  and compiled into the firmware (`secrets.h`, gitignored and never committed).
- **A fresh session key for every connection:** each side sends a random nonce, and
  `session = HMAC(key, "jarvis-session|client_nonce|device_nonce")`.
- **Every line is signed in both directions:** `<seq> <mac> <payload>`, where
  `mac = HMAC(session, "c|seq|payload")` (the board uses `d`).
  - Sequence numbers must increase by exactly one, so a replayed line is rejected.
  - Each direction uses its own prefix, so a line can't be bounced back at its sender.
  - MACs are compared in constant time.
- **A forged line ends the session:** a forged event (someone faking a tap to wake the mic) closes the
  connection. A client that can't prove the key within 5 seconds is cut off, and the board accepts
  one client at a time.
- **The host accepts only fixed command shapes** (`[a-z]+` plus up to three small integers), so a
  command can't smuggle in a second line.
- **The relay only moves when the user's own words ask for it.** A question ("is the relay on?") or
  the model's own idea is refused.
- **Unwired sensors report `null`** (the firmware detects a floating pin) and are never turned into
  made-up numbers.

## Run it

```bash
pip install -r requirements.txt
python3 -m unittest tests.test_sensors -v     # protocol and end-to-end tests against a simulated board
```

To use a real board you also need the Arduino ESP32 core (3.x) and arduino-cli. Then:

```bash
python3 sensors.py flash     # creates the key, compiles, and uploads (detects S3 vs classic ESP32)
python3 sensors.py test      # handshake, a reading, a beep and an LED check
```

Wiring, power options and Wi-Fi setup are in [esp32/README.md](esp32/README.md). How the full
assistant hooks into these files is described in [INTEGRATION.md](INTEGRATION.md).

## Files

| File | What it is |
|---|---|
| `esp32/jarvis_sensors/jarvis_sensors.ino` | Firmware: sensors, actuators, signed protocol, Wi-Fi/mDNS |
| `esp32/jarvis_sensors/config.h` | Which parts are connected, and the pins for ESP32-S3 and classic ESP32 |
| `esp32/jarvis_sensors/session.h` | The per-connection session state |
| `esp32/jarvis_sensors/secrets.example.h` | Template for the key and Wi-Fi (`secrets.h` is generated) |
| `sensors.py` | Mac side: key setup, flashing, the signed link, the background hub |
| `tools/sensors.py` | The assistant's hardware tools and the phrasing-to-command matcher |
| `tools/__init__.py` | A minimal stand-in for Jarvis's tool registry, so this repo runs alone |
| `tests/test_sensors.py` | Tests against a fake board written separately from the firmware |
