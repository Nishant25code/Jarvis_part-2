# Jarvis sensor hub (ESP32)

An ESP32 with a few sensors gives Jarvis senses in your room:

- **Tap to talk:** touch the pad and Jarvis starts listening, so the microphone can stay fully off the rest of the time.
- **Room readings:** temperature, humidity, light, and whether you're at your desk.
- **Status light and buzzer:** an RGB light shows what Jarvis is doing, and the buzzer is there for signals and alerts.
- **A switch:** a relay turns a lamp or fan on and off.

Everything stays on your network. Every message is signed with a key that only this Mac and the board know. Nobody else on your Wi-Fi can read the sensors, flip the relay, or fake a tap that wakes Jarvis.

## Parts to use from the 45-in-1 kit

| Part | What Jarvis does with it | ESP32-S3 pin (yours) |
|---|---|---|
| **Touch sensor** (TTP223) | Tap to wake Jarvis, like saying "Hey Jarvis" | SIG → **GPIO7**, VCC → 3V3 |
| **DHT11** temperature & humidity | "How warm is it in here?" | DATA → **GPIO4**, VCC → 3V3 |
| **Photoresistor (LDR)** module | "Is it dark in here?" | AO/S → **GPIO1**, VCC → 3V3 |
| **HC-SR04** ultrasonic | Knows if you're at your desk | TRIG → **GPIO5**, ECHO → **GPIO6** (see the note below), VCC → 5V |
| **Status light** | Blue listening, purple thinking, green speaking | Uses the S3's **built-in RGB LED**: nothing to wire. For the kit's KY-016 module instead, set `LED_ONBOARD 0` in config.h and wire R → GPIO10, G → GPIO11, B → GPIO12. |
| **Active buzzer** | Beeps on request | S → **GPIO13** |
| **Relay module** | Switches a lamp or fan | IN/S → **GPIO14**, VCC → 5V |

Every GND goes to an ESP32 GND pin. On a classic ESP32 (not S3) the pins are different; `config.h` has both, and `flash` picks the right set for the chip it finds.

Sensors that aren't wired yet read as "not connected", never as made-up numbers. Wire one up and it starts working; no reflash needed.

**Optional:** switch any of these on in `jarvis_sensors/config.h` by setting it to `1`.

| Part | Use | Pin |
|---|---|---|
| Soil moisture | Tells you when a plant needs water | AO → GPIO2 |
| Reed switch | Door open/closed (put a magnet on the door) | S → GPIO15 |
| Flame sensor | Alerts you aloud. Cheap IR flame sensors can trigger in sunlight, and they are **not** a smoke alarm. | DO → GPIO16 |

The rest of the kit has no obvious job for Jarvis yet: MPU6050, joystick, IR, laser, sound sensors, encoder and so on.

### Notes

- **HC-SR04 echo is 5 V** and the ESP32 is a 3.3 V board. Use a divider: ECHO → 1 kΩ → GPIO6, and GPIO6 → 2 kΩ → GND. With no resistors, power the HC-SR04 from 3V3 instead. Many work that way. If the distance always reads "no echo", go back to 5 V with the divider.
- **The relay is for low-voltage things only:** a USB lamp, an LED strip, or a 5–12 V fan. Don't wire mains (230 V) through a kit relay on your desk.
- If the relay is on when Jarvis says off, set `RELAY_ACTIVE_LOW 1` in `config.h`.
- If the light reading is backwards, flip `LIGHT_INVERT`.
- Unplugged parts: set them to `0` in `config.h` so Jarvis never reads an empty pin.

## Power

- **USB cable to the Mac** gives power and data together. Nothing else is needed.
- **Wi-Fi:** the board can be anywhere. Power it from any USB phone charger (5 V, 1 A or more) or a power bank, through its normal USB port.
  - The kit's breadboard power module works too: 5 V into the ESP32's **5V** pin.
  - So does the MP1584 buck converter, set to 5 V from a 9–12 V adapter.
  - Never feed more than 5 V into VIN.

## Set up

1. Plug the ESP32 into the Mac with a **data** USB cable. Some cables only charge.
2. Flash it:

   ```bash
   python3 sensors.py flash
   ```

   This creates the shared key in `~/.jarvis/sensors.json` and `jarvis_sensors/secrets.h`, both readable only by you. It then compiles and uploads.
3. Check it:

   ```bash
   python3 sensors.py test
   ```

4. Restart Jarvis. It finds the board by itself.

**Wi-Fi (optional):**

1. Open `jarvis_sensors/secrets.h` and type your Wi-Fi name and password into `WIFI_SSID` and `WIFI_PASS` yourself.
2. Run `flash` again.
3. Unplug the board and power it anywhere. Jarvis reaches it at `jarvis-sensors.local`.

USB is always tried first. The board accepts one Jarvis at a time, and a connection that can't prove the key within 5 seconds is cut off.

## Talking to it

- "What's the temperature in here?"
- "Am I at my desk?"
- "Is it dark in the room?"
- **Relay:** "Turn on the relay", "Can you switch the relay off?", "Relay on", "Toggle the relay", "Is the relay on?" (only reads it).
  Name what it switches in `~/.jarvis/sensors.json` with `"relay_name": "desk lamp"`, then "turn on the desk lamp" works too.
- **Status light:** "Make the light violet", "Set the LED to red", "Turn the status light off", "Set the light back to normal".
  Colours: red, green, blue, white, warm white, yellow, orange, purple, pink, cyan (also violet, teal, aqua, magenta, amber, gold, lime).
- **Buzzer:** "Beep", "Buzz three times", "Make a noise", "Give me a long beep", "Beep twice for a second" (1–5 beeps, each up to 2 seconds).

**Tap to talk with the microphone off:**

1. In Jarvis's menu, set Listening to **Microphone off: ⌥Space to talk**.
2. The mic now stays completely off until you tap the touch sensor or press ⌥Space.
