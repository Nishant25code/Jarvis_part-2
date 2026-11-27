"""Room sensors on the ESP32 hub (see sensors.py and esp32/README.md)."""

import re

import tools as _tools

from . import tool

COLOURS = {"off": (0, 0, 0), "red": (255, 0, 0), "green": (0, 200, 0), "blue": (0, 60, 255),
           "white": (255, 255, 255), "warm white": (255, 140, 40), "yellow": (255, 180, 0),
           "orange": (255, 90, 0), "purple": (130, 0, 220), "pink": (255, 40, 120), "cyan": (0, 200, 200)}


# Other names people use for the colours above.
COLOUR_WORDS = {"violet": "purple", "magenta": "pink", "teal": "cyan", "aqua": "cyan", "turquoise": "cyan",
                "amber": "orange", "gold": "yellow", "golden": "yellow", "warm": "warm white", "lime": "green"}
_AUTO = re.compile(r"\b(auto(matic)?|normal|default|usual|reset|back to (normal|auto|how it was))\b", re.I)
_LIGHT = re.compile(r"\b(status light|led|rgb|light|glow|colou?r)\b", re.I)
_SENSE = re.compile(r"\b(dark|bright(ness)?|light level|light sensor|how much light|sunlight|lux)\b", re.I)
_BUZZ = re.compile(r"\b(beep\w*|buzz\w*|bleep\w*|chirp\w*|honk\w*|ding)\b|\bmake (a |some )?(noise|sound)\b|"
                   r"\b(sound|ring|test|use|try|play)\b.{0,25}\b(buzzer|beeper)\b|\b(sound|ring) (the|an|a) alarm\b", re.I)
_QUESTION = re.compile(r"^\W*(jarvis\W+)?(is|are|was|what|which|does|did|do|has|have|why|how)\b", re.I)
_COUNT_WORDS = {"once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "a couple of": 2, "a couple": 2, "a few": 3, "several": 4}


_CONTINUOUS = re.compile(r"\b(continuous\w*|non-?stop|repeatedly|keep (on )?(beep|buzz)\w*|again and again|a lot|many times)\b", re.I)


def _count(text: str):
    if _CONTINUOUS.search(text):
        return 5                     # the most the board plays in one go
    m = re.search(r"\b(\d+)\s*(?:times|x|beeps?|buzz\w*)\b|\b(?:beep|buzz)\w*\s+(\d+)\b", text, re.I)
    if m:
        return int(m.group(1) or m.group(2))
    for word, n in sorted(_COUNT_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b(?!\s+(?:seconds?|secs?|s)\b)", text, re.I) and (
                word in ("once", "twice", "thrice") or re.search(rf"\b{word}\s+(?:\w+\s+)?(times|beeps?|buzz\w*)\b", text, re.I)):
            return n
    return 1


def _seconds(text: str) -> float:
    m = re.search(r"\b(\d+(?:\.\d+)?|half a|a|one|two)\s*(?:-\s*)?(seconds?|secs?|s)\b", text, re.I)
    if m:
        v = {"half a": 0.5, "a": 1, "one": 1, "two": 2}.get(m.group(1).lower())
        return min(2.0, v if v is not None else float(m.group(1)))
    if re.search(r"\b(long|longer|loud|slow)\b", text, re.I):
        return 0.8
    if _CONTINUOUS.search(text):
        return 0.5
    return 0.15


def board_intent(text: str):
    """A plain request for the buzzer or the status light, as (tool, args); None if it's anything else.
    Lets Jarvis act on however it's phrased ("buzz three times", "make the led violet"), not only exact wording."""
    if not text:
        return None
    relay = _relay_intent(text)
    if relay:
        return relay
    if not _QUESTION.search(text):            # "is the light red?" asks; it doesn't set anything
        action = _action_intent(text)
        if action:
            return action
    about = _read_focus(text)
    return ("room_sensors", {"about": about}) if about else None


def _action_intent(text: str):
    if _BUZZ.search(text):
        return "sensor_beep", {"times": _count(text), "seconds": _seconds(text)}
    if _LIGHT.search(text) and not _SENSE.search(text):
        words = sorted(list(COLOURS) + list(COLOUR_WORDS), key=len, reverse=True)
        m = re.search(r"\b(" + "|".join(words) + r")\b", text, re.I)
        colour = m and COLOUR_WORDS.get(m.group(1).lower(), m.group(1).lower())
        if colour and colour != "off" and not re.search(r"\b(turn|switch|put)\b.{0,20}\boff\b", text, re.I):
            return "set_status_light", {"colour": colour}
        if _AUTO.search(text):
            return "set_status_light", {"colour": "auto"}
        if re.search(r"\b(status light|led|rgb)\b", text, re.I):
            if re.search(r"\boff\b|\b(stop|kill|disable)\b", text, re.I):
                return "set_status_light", {"colour": "off"}
            if re.search(r"\bon\b|\b(turn|switch|light) (it )?up\b", text, re.I):
                return "set_status_light", {"colour": "white"}
    return None


def _hub():
    import sensors
    h = sensors.hub
    if h is None:
        raise RuntimeError("No sensor board is set up. Run `python3 sensors.py flash` with the ESP32 plugged in.")
    if not h.connected:
        raise RuntimeError(f"The sensor board isn't connected right now ({h.problem}).")
    return h


def _num(value):
    """Readings are numbers or null; anything else from the board is ignored."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def describe(r: dict, presence_cm: float = 120, relay_name: str = "relay") -> str:
    parts = []
    t, h = _num(r.get("temp_c")), _num(r.get("humidity"))
    missing = []
    if "temp_c" in r:
        if t is None:
            missing.append("temperature/humidity (DHT11)")
        else:
            parts.append(f"Room temperature {t:.1f}°C")
    if h is not None:
        parts.append(f"humidity {h:.0f}%")
    light = _num(r.get("light"))
    if "light" in r and light is None:
        missing.append("light sensor")
    if light is not None:
        word = "dark" if light < 15 else "dim" if light < 40 else "fairly bright" if light < 70 else "bright"
        parts.append(f"light {light:.0f}% ({word})")
    if "distance_cm" in r:
        d = _num(r.get("distance_cm"))
        if d is None:
            missing.append("distance sensor (or nothing within 4 m)")
        elif d <= presence_cm:
            parts.append(f"someone is at the desk ({d:.0f} cm away)")
        else:
            parts.append(f"nobody at the desk (nearest thing {d:.0f} cm away)")
    if "soil" in r and r.get("soil") is None:
        missing.append("soil sensor")
    soil = _num(r.get("soil"))
    if soil is not None:
        parts.append(f"plant soil moisture {soil:.0f}%" + (" (dry, needs water)" if soil < 30 else ""))
    if isinstance(r.get("door_open"), bool):
        parts.append("door open" if r["door_open"] else "door closed")
    if r.get("flame") is True:
        parts.append("THE FLAME SENSOR IS TRIGGERED")
    if isinstance(r.get("relay"), bool):
        parts.append(f"{relay_name} {'on' if r['relay'] else 'off'}")
    text = (", ".join(parts)[:1].upper() + ", ".join(parts)[1:] + ".") if parts else ""
    if missing:
        text += (" " if text else "") + ("Not connected or not answering: " + ", ".join(missing) +
                 ". (Check the wiring in esp32/README.md; there is nothing for you to switch or restart.)")
    return text or "The board answered but has no sensors switched on in config.h."


_SWITCH_VERB = r"\b(turn|switch|power|flip|toggle|put|start|stop|kill|cut|activate|deactivate|enable|disable)\b"
_STATE_QUESTION = r"\W*(?:hey\W+)?(?:jarvis\W+)?(is|are|was|were|what|which|did|does|why|how)\b"


def _relay_words(name: str) -> list:
    return [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2] + ["relay", "rele", "relays", "lamp", "fan", "plug", "socket", "strip"]


def _asked_to_switch(name: str) -> bool:
    """The relay moves real things (a lamp, a fan), so only when the user's own words ask for it.
    "Can you turn on the lamp?" is a request; "is the lamp on?" is a question."""
    said = _tools.user_text.lower()
    question = re.match(_STATE_QUESTION, said)
    command = (re.search(_SWITCH_VERB, said) and not question) or (not question and re.search(r"\b(on|off)\W*$", said))
    return bool(command) and any(re.search(rf"\b{w}\b", said) for w in _relay_words(name))   # never a bare "it": "turn it off" may be the music


def _relay_intent(text: str):
    import sensors
    h = sensors.hub
    name = h.cfg.get("relay_name", "relay") if h is not None else "relay"
    said = text.lower()
    if re.match(_STATE_QUESTION, said) or not any(re.search(rf"\b{w}\b", said) for w in _relay_words(name)):
        return None
    if not (re.search(_SWITCH_VERB, said) or re.search(r"\b(on|off)\W*$", said)):
        return None
    if re.search(r"\b(off|stop|kill|cut|deactivate|disable)\b", said):
        return "switch_relay", {"on": False}
    if re.search(r"\b(on|start|activate|enable)\b|\bpower (it )?up\b", said):
        return "switch_relay", {"on": True}
    if re.search(r"\b(toggle|flip)\b", said) and h is not None and h.connected:
        try:
            state = h.call("read").get("relay")
        except Exception:
            return None
        if isinstance(state, bool):
            return "switch_relay", {"on": not state}
    return None


@tool
def room_sensors(about: str = "all") -> str:
    """Read the sensors in the user's room (ESP32 hub): light level, the relay, the touch pad, and
    temperature/desk sensors if fitted. Use this for anything about the room itself (not the weather
    outside), and answer only what was asked.

    Args:
        about: all, light, relay, touch, temperature or presence
    """
    h = _hub()
    r = h.call("read")
    focus = (about or "all").strip().lower()
    if focus != "all":
        answer = _focused(focus, r, h.cfg.get("relay_name", "relay"))
        if answer:
            return answer
    text = describe(r, float(h.cfg.get("presence_cm", 120)), h.cfg.get("relay_name", "relay"))
    return text + _board_extras(h, r)


def _focused(focus: str, r: dict, relay_name: str):
    """One straight answer to one question ("how dark is it?" gets the light, nothing else)."""
    if focus == "light":
        if "light" not in r:
            return "There's no light sensor on the board."
        light = _num(r.get("light"))
        if light is None:
            return "The light sensor isn't answering. Check its wire to GPIO1."
        word = "dark" if light < 15 else "dim" if light < 40 else "fairly bright" if light < 70 else "bright"
        return f"It's {word} in here: the light sensor reads {light:.0f}%."
    if focus == "relay":
        state = r.get("relay")
        return f"The {relay_name} is {'on' if state else 'off'}." if isinstance(state, bool) else "The relay isn't answering."
    if focus == "touch":
        state = r.get("touch")
        if not isinstance(state, bool):
            return "There's no touch pad on the board."
        return "Someone is touching the touch pad right now." if state else "The touch pad is ready: tap it to wake me."
    if focus == "temperature":
        if "temp_c" not in r:
            return "No temperature sensor is connected to the board."
        t, hum = _num(r.get("temp_c")), _num(r.get("humidity"))
        if t is None:
            return "The temperature sensor isn't answering."
        return f"It's {t:.1f}°C in here" + (f", humidity {hum:.0f}%." if hum is not None else ".")
    if focus == "presence":
        if "distance_cm" not in r:
            return "No distance sensor is connected, so I can't tell if you're at the desk."
        d = _num(r.get("distance_cm"))
        return "Nothing is in front of the distance sensor." if d is None else f"Something is {d:.0f} cm from the distance sensor."
    return None


_ROOM_CONTEXT = r"\b(room|in here|inside|indoors?|my desk|board|esp32)\b"


def _read_focus(text: str):
    """Which reading a question is about, or None if it isn't about the board."""
    said = text.lower()
    if _SENSE.search(said):
        return "light"
    import sensors
    h = sensors.hub
    name = h.cfg.get("relay_name", "relay") if h is not None else "relay"
    if any(re.search(rf"\b{w}\b", said) for w in _relay_words(name)):
        return "relay"
    if re.search(r"\btouch ?(pad|sensor)\b", said):
        return "touch"
    if re.search(r"\b(temperature|warm|cold|hot|humid\w*)\b", said) and re.search(_ROOM_CONTEXT, said):
        return "temperature"
    if re.search(r"\b(at (my|the) desk|in front of)\b", said):
        return "presence"
    if re.search(r"\b(room )?sensors?\b|\b(board|esp32) status\b|\bstatus of the (board|room)\b|\bwhat'?s connected\b", said):
        return "all"
    return None


def _board_extras(h, r: dict) -> str:
    """The rest of the board in one line: touch pad, status light, buzzer."""
    parts = h.info.get("parts", []) if isinstance(h.info, dict) else []
    extra = []
    if "touch" in parts and isinstance(r.get("touch"), bool):
        extra.append("touch pad " + ("being touched" if r["touch"] else "ready (tap it to wake me)"))
    if "led" in parts:
        if getattr(h, "_light_auto", True):
            extra.append("status light on automatic")
        else:
            rgb = tuple(getattr(h, "_colour", None) or ())
            name = next((n for n, c in COLOURS.items() if c == rgb), "a custom colour")
            extra.append("status light " + ("off" if name == "off" else f"set to {name}"))
    if "buzzer" in parts:
        extra.append("buzzer ready")
    return (" " + ", ".join(extra)[:1].upper() + ", ".join(extra)[1:] + ".") if extra else ""


@tool(sensitive=True)
def switch_relay(on: bool) -> str:
    """Switch the device wired to the ESP32 relay on or off (the user may call it by name, e.g. desk lamp or fan).

    Args:
        on: true to switch it on, false to switch it off
    """
    h = _hub()
    name = h.cfg.get("relay_name", "relay")
    if not _asked_to_switch(name):
        state = h.call("read").get("relay")
        now = f" The {name} is currently {'on' if state else 'off'}." if isinstance(state, bool) else ""
        return (f"Not switched: the user didn't ask to switch the {name}. Only use this when they ask "
                f"to turn the {name} on or off.{now}")
    want = _relay_intent(_tools.user_text)        # the direction comes from the user's words, not the model
    if not want or want[1]["on"] != bool(on):
        said = ("on" if want[1]["on"] else "off") if want else "neither on nor off clearly"
        return f"Not switched: the user asked for {said}, not {'on' if on else 'off'}."
    reply = h.call(f"relay {1 if on else 0}")
    return f"Switched the {name} {'on' if reply.get('relay') else 'off'}."


@tool
def set_status_light(colour: str) -> str:
    """Set the RGB light on the ESP32 hub to a colour, or 'auto' to go back to showing Jarvis's state
    (blue listening, purple thinking, green speaking, off when idle).

    Args:
        colour: auto, off, red, green, blue, white, warm white, yellow, orange, purple, pink or cyan
    """
    h = _hub()
    name = colour.strip().lower()
    if name == "auto":
        h.set_light((0, 0, 0), auto=True)
        return "The light is back to showing what I'm doing."
    if name not in COLOURS:
        return "I can do: " + ", ".join(["auto"] + list(COLOURS)) + "."
    h.set_light(COLOURS[name], auto=False)
    return f"Light set to {name}." if name != "off" else "Light off."


@tool
def sensor_beep(times: int = 1, seconds: float = 0.15) -> str:
    """Beep or buzz the buzzer on the ESP32 board: any request to beep, buzz, ring the buzzer or make a noise.

    Args:
        times: how many beeps, 1 to 5
        seconds: how long each beep lasts, 0.05 to 2 (0.15 is a short beep, 1 is a long one)
    """
    n = max(1, min(5, int(times)))
    ms = int(max(0.05, min(2.0, float(seconds))) * 1000)
    _hub().call(f"beep {ms} {n}")
    length = "long " if ms >= 600 else ""
    return f"Beeped {n} {length}time{'s' if n > 1 else ''}." if n > 1 else f"Gave one {length}beep."


@tool
def sensor_hub_status() -> str:
    """Whether the ESP32 sensor board is connected, how (USB or Wi-Fi), and which parts it has."""
    import sensors
    h = sensors.hub
    if h is None:
        return "No sensor board is set up yet."
    if not h.connected:
        return f"The sensor board isn't connected ({h.problem})."
    info = h.info
    parts = ", ".join(p for p in info.get("parts", []) if isinstance(p, str))
    wifi = info.get("wifi")
    extra = f"; Wi-Fi {wifi}" if wifi and wifi != "off" else ""
    return f"Connected over {h.via}, firmware {info.get('fw', '?')}; parts: {parts or 'none'}{extra}."
