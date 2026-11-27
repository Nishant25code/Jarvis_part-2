# How the assistant uses this hardware

The full Jarvis code isn't in this repository. These are the only places where it touches the hardware.

## 1. Start the hub when Jarvis starts

```python
import sensors
sensors.start(on_event=on_sensor_event, log=print)   # background thread: connect, reconnect, keep alive
```

## 2. A tap on the touch sensor = "Hey Jarvis"

```python
def on_sensor_event(name, data):
    """Events from the ESP32 (signed, so they really came from the board)."""
    if name == "touch" and time.time() - last_touch > 2:   # at most one wake-up every 2 s
        inbox.put("wake")                                  # same as hearing the wake word
    elif name == "flame":
        announce("Warning: the flame sensor has detected a flame.")
```

## 3. The status light follows Jarvis's state

```python
# whenever the assistant changes state (listening / thinking / speaking / idle)
sensors.hub.show_state(state)       # blue / purple / green / off, unless the user picked a colour
```

## 4. Requests reach the right tool, however they're worded

Before the language model sees a request, `tools.sensors.board_intent(text)` checks whether it's a
plain hardware command:

| Said | Exact call |
|---|---|
| "can you turn on the relay?" | `switch_relay(on=True)` |
| "buzz three times", "keep beeping" | `sensor_beep(times=3)`, `sensor_beep(times=5, seconds=0.5)` |
| "make the LED violet", "light back to normal" | `set_status_light("purple")`, `set_status_light("auto")` |
| "is the relay on?", "set an alarm for 7am" | nothing: a question or not about the board |

If it matches, the model is told the exact call. If the model still doesn't make it, the assistant
makes the call itself, so the user never gets "done" without the hardware moving. Room questions
("is it dark in here?") must be answered from a live `room_sensors` reading, never from memory, and
a reply that claims the relay was switched or the buzzer beeped is checked against the tools that
actually ran.
