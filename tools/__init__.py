"""A minimal stand-in for Jarvis's tool registry, so the hardware code in this repo runs on its own.

In the full assistant this package also routes tool calls from the language model, checks
permissions, and holds every other tool. Only the two things tools/sensors.py uses are kept here:
the @tool decorator and `user_text` (the user's own words for the current request)."""

TOOLS = {}
user_text = ""          # set by the assistant before each request; the relay checks it


def tool(fn=None, *, confirm=False, sensitive=False, untrusted=False, outbound=()):
    def register(f):
        TOOLS[f.__name__] = {"fn": f, "confirm": confirm, "sensitive": sensitive}
        return f
    return register(fn) if fn is not None else register


def run(name: str, args: dict) -> str:
    try:
        return str(TOOLS[name]["fn"](**(args or {})))
    except Exception as e:
        return f"Error running {name}: {e}"
