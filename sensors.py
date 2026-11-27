"""Link to the ESP32 sensor hub (firmware in esp32/jarvis_sensors).

The board is reached over the USB cable or, if you gave it your Wi-Fi details, over
your home network at jarvis-sensors.local. Both use the same signed protocol: a
shared 256-bit key (made on this Mac by `setup`), a fresh session key per connection,
and an HMAC on every line in both directions, so nobody else on the network can read
the sensors, switch the relay, or fake a touch that wakes Jarvis.

    python3 sensors.py setup     make the key and esp32/jarvis_sensors/secrets.h
    python3 sensors.py flash     compile and upload the firmware (board on USB)
    python3 sensors.py test      connect and print what the board sees
"""

import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home() / ".jarvis"
CONFIG = HOME / "sensors.json"
FIRMWARE = Path(__file__).resolve().parent / "esp32" / "jarvis_sensors"
SECRETS_H = FIRMWARE / "secrets.h"
BUILD_DIR = HOME / "tmp" / "esp32-build"
FQBN = "esp32:esp32:esp32"
DEFAULT_HOST = "jarvis-sensors.local"
DEFAULT_PORT = 7878
SERIAL_GLOBS = ("/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*")
FRAME = re.compile(r"\d{1,10} [0-9a-f]{32} \{")
ARDUINO_CLI = ("/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli",)

# Status light colours for Jarvis's states (R, G, B). Dim on purpose: it sits on a desk.
STATE_COLOURS = {"idle": (0, 0, 0), "listening": (0, 60, 255), "thinking": (120, 0, 200),
                 "speaking": (0, 160, 90), "approval": (255, 110, 0), "working": (120, 0, 200)}


# ── Config ────────────────────────────────────────────────────────────────

def load_config():
    """~/.jarvis/sensors.json, only if it's ours and private. None if setup hasn't been run."""
    try:
        st = CONFIG.stat()
    except FileNotFoundError:
        return None
    if st.st_uid != os.getuid():
        raise PermissionError(f"{CONFIG} isn't owned by you; not using it")
    if st.st_mode & 0o077:
        os.chmod(CONFIG, 0o600)
    cfg = json.loads(CONFIG.read_text())
    if not re.fullmatch(r"[0-9a-f]{64}", str(cfg.get("key", ""))):
        raise ValueError(f"{CONFIG} has no valid key; run `python3 sensors.py setup`")
    return cfg


def _write_private(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def setup() -> dict:
    """Create the shared key (once) and write it into the firmware's secrets.h,
    keeping any Wi-Fi name and password already typed there."""
    try:
        cfg = load_config() or {}
    except ValueError:
        cfg = json.loads(CONFIG.read_text())
    if not re.fullmatch(r"[0-9a-f]{64}", str(cfg.get("key", ""))):
        cfg["key"] = secrets.token_hex(32)
    cfg.setdefault("host", DEFAULT_HOST)
    cfg.setdefault("relay_name", "relay")
    cfg.setdefault("status_light", True)
    cfg.setdefault("presence_cm", 120)
    _write_private(CONFIG, json.dumps(cfg, indent=2) + "\n")

    template = (FIRMWARE / "secrets.example.h").read_text()
    current = SECRETS_H.read_text() if SECRETS_H.exists() else template
    text = re.sub(r'#define JARVIS_KEY "[^"]*"', f'#define JARVIS_KEY "{cfg["key"]}"', current)
    _write_private(SECRETS_H, text)
    return cfg


# ── Protocol (no I/O, so it's easy to test) ───────────────────────────────

class Session:
    def __init__(self, master: bytes):
        self.master = master
        self.cnonce = secrets.token_hex(16)
        self.key = None
        self.tx = self.rx = 0

    def hello(self) -> str:
        return f"hello {self.cnonce}"

    def accept_hello(self, line: str) -> bool:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "hello" or not re.fullmatch(r"[0-9a-f]{32}", parts[1]):
            return False
        self.key = hmac.new(self.master, f"jarvis-session|{self.cnonce}|{parts[1]}".encode(), hashlib.sha256).digest()
        self.tx = self.rx = 0
        return True

    def _mac(self, direction: str, seq: int, payload: str) -> str:
        return hmac.new(self.key, f"{direction}|{seq}|{payload}".encode(), hashlib.sha256).hexdigest()[:32]

    def seal(self, payload: str) -> str:
        if "\n" in payload or "\r" in payload:
            raise ValueError("one line only")
        self.tx += 1
        return f"{self.tx} {self._mac('c', self.tx, payload)} {payload}"

    def open(self, line: str):
        """The payload of a correctly signed, in-order line from the board, else None."""
        parts = line.split(" ", 2)
        if self.key is None or len(parts) != 3 or not parts[0].isdigit():
            return None
        seq = int(parts[0])
        if seq != self.rx + 1 or not hmac.compare_digest(parts[1], self._mac("d", seq, parts[2])):
            return None
        self.rx = seq
        return parts[2]


class Forged(Exception):
    """The board's reply wasn't signed with our key (wrong firmware key, or someone else)."""


# ── Transports ────────────────────────────────────────────────────────────

def serial_ports() -> list:
    found = []
    for pattern in SERIAL_GLOBS:
        found += sorted(glob.glob(pattern))
    return found


class _SerialLink:
    via = "USB"

    def __init__(self, port: str):
        import serial                              # pyserial: only needed for the USB cable
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, 115200, 0.5
        s.dtr = s.rts = False                     # don't hold the board in reset
        s.open()
        self.s, self.name = s, port
        self._buf = b""

    def write(self, line: str):
        self.s.write(line.encode() + b"\n")
        self.s.flush()

    def readline(self):
        """One line, or None after a short timeout."""
        while b"\n" not in self._buf:
            chunk = self.s.read(max(1, self.s.in_waiting))   # whatever has arrived; wait only if nothing has
            if not chunk:
                return None
            self._buf += chunk
            if len(self._buf) > 4096:
                self._buf = b""
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


class _TcpLink:
    via = "Wi-Fi"

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.5)
        self.name = f"{host}:{port}"
        self._buf = b""

    def write(self, line: str):
        self.sock.sendall(line.encode() + b"\n")

    def readline(self):
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(1024)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("board closed the connection")
            self._buf += chunk
            if len(self._buf) > 4096:
                raise ConnectionError("line too long")
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ── Hub ───────────────────────────────────────────────────────────────────

class Hub:
    """Keeps a signed connection to the board in the background, reconnecting as needed."""

    def __init__(self, cfg: dict, on_event=None, log=print):
        self.cfg = cfg
        self.master = bytes.fromhex(cfg["key"])
        self.on_event = on_event
        self.log = log
        self.link = None
        self.session = None
        self.info = {}
        self.problem = "not connected yet"
        self._lock = threading.Lock()
        self._waiting = {}
        self._next_id = 0
        self._stop = threading.Event()
        self._light_auto = True
        self._colour = None

    # public
    @property
    def connected(self) -> bool:
        return self.link is not None and self.session is not None and self.session.key is not None and bool(self.info)

    @property
    def via(self) -> str:
        return self.link.via if self.link else ""

    def start(self):
        threading.Thread(target=self._run, name="sensors", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._drop()

    def call(self, command: str, timeout: float = 4.0) -> dict:
        if not re.fullmatch(r"[a-z]+( -?\d{1,5}){0,3}", command):
            raise ValueError("bad command")
        link, session = self.link, self.session
        if not link or not session or session.key is None:
            raise RuntimeError(self.problem or "the sensor board isn't connected")
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            slot = {"event": threading.Event(), "reply": None}
            self._waiting[rid] = slot
            try:
                link.write(session.seal(f"{rid} {command}"))
            except Exception as e:
                self._waiting.pop(rid, None)
                self._drop(f"lost the board: {e}")
                raise RuntimeError("lost the connection to the sensor board") from e
        if not slot["event"].wait(timeout):
            self._waiting.pop(rid, None)
            raise TimeoutError("the sensor board didn't answer")
        reply = slot["reply"]
        if reply is None:
            raise RuntimeError("lost the connection to the sensor board")
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "the board refused that"))
        return reply

    def show_state(self, state: str):
        """Jarvis's state on the RGB LED (listening blue, thinking purple...)."""
        if not self._light_auto or not self.cfg.get("status_light", True) or not self.connected:
            return
        colour = STATE_COLOURS.get(state)
        if colour is None or colour == self._colour:
            return
        self._colour = colour
        threading.Thread(target=self._quiet_call, args=("led %d %d %d" % colour,), daemon=True).start()

    def set_light(self, rgb, auto: bool = False):
        self._light_auto = auto
        self._colour = tuple(rgb)
        return self.call("led %d %d %d" % tuple(rgb))

    # internals
    def _quiet_call(self, command):
        try:
            self.call(command, timeout=2)
        except Exception:
            pass

    def _candidates(self):
        port = self.cfg.get("port")
        if port:
            yield lambda: _SerialLink(port)
        else:
            for p in serial_ports():
                yield (lambda p=p: _SerialLink(p))
        host = self.cfg.get("host")
        if host:
            yield lambda: _TcpLink(host, int(self.cfg.get("tcp_port", DEFAULT_PORT)))

    def _handshake(self, link) -> bool:
        session = Session(self.master)
        deadline = time.time() + 4
        sent_at = 0
        while time.time() < deadline:
            if time.time() - sent_at > 1.5:        # the board may still be booting after the port opened
                link.write(session.hello())
                sent_at = time.time()
            line = link.readline()
            if line and session.accept_hello(line):
                self.link, self.session = link, session
                return True
        return False

    def _connect(self) -> bool:
        tried = []
        for make in self._candidates():
            try:
                link = make()
            except Exception as e:
                tried.append(str(e).split("\n")[0][:80])
                continue
            try:
                if self._handshake(link):
                    reader = threading.Thread(target=self._read, args=(link,), name="sensors-read", daemon=True)
                    reader.start()
                    self.info = {}
                    info = self.call("info")
                    self.info = info
                    self.problem = ""
                    self.log(f"Sensor board connected over {link.via} ({', '.join(info.get('parts', []))}).")
                    if self.cfg.get("status_light", True):
                        self._quiet_call("led 0 0 0")
                    return True
                tried.append(f"{link.name}: no answer")
            except Exception as e:
                tried.append(f"{link.name}: {e}")
            self._drop()
            link.close()
        problem = "sensor board not found" + (f" ({'; '.join(tried)})" if tried else
                                                " (plug it in by USB, or give it Wi-Fi details)")
        if problem != self.problem:
            self.log(f"Sensor board: {problem}")
        self.problem = problem
        return False

    def _read(self, link):
        try:
            while self.link is link and not self._stop.is_set():
                line = link.readline()
                if not line or self.link is not link:
                    continue
                if not FRAME.match(line):
                    continue                         # boot messages and USB noise, not a signed line
                payload = self.session.open(line)
                if payload is None:
                    raise Forged("a message from the board wasn't signed with Jarvis's key")
                msg = json.loads(payload)
                if "event" in msg:
                    if self.on_event:
                        try:
                            self.on_event(str(msg["event"]), msg)
                        except Exception:
                            pass
                    continue
                slot = self._waiting.pop(msg.get("id"), None)
                if slot:
                    slot["reply"] = msg
                    slot["event"].set()
        except Forged as e:
            self._drop(f"{e}. Run `python3 sensors.py flash` so the board has the current key.")
        except Exception as e:
            if not self._stop.is_set() and self.link is link:   # not just us shutting down
                self._drop(f"lost the board: {e}")

    def _drop(self, problem: str = None):
        link = self.link
        self.link = self.session = None
        self.info = {}
        self._colour = None
        if problem:
            if problem != self.problem:
                self.log(f"Sensor board: {problem}")
            self.problem = problem
        for slot in list(self._waiting.values()):
            slot["event"].set()
        self._waiting.clear()
        if link:
            link.close()

    def _run(self):
        wait = 3
        while not self._stop.is_set():
            if not self.connected and self.link is None:
                if self._connect():
                    wait = 3
                else:
                    self._stop.wait(wait)
                    wait = min(wait * 2, 60)
                    continue
            if self._stop.wait(10):
                break
            if self.connected:
                try:
                    self.call("ping", timeout=4)
                except Exception as e:
                    self._drop(f"stopped answering ({e})")


hub = None


def start(on_event=None, log=print):
    """Start the background link if `setup` has been run. Returns the Hub or None."""
    global hub
    cfg = load_config()
    if cfg is None:
        return None
    hub = Hub(cfg, on_event=on_event, log=log)
    hub.start()
    return hub


# ── Command line: setup, flash, test ─────────────────────────────────────

def _arduino_cli():
    found = shutil.which("arduino-cli")
    if found:
        return found
    for path in ARDUINO_CLI:
        if os.access(path, os.X_OK):
            return path
    sys.exit("arduino-cli not found. Install the Arduino IDE (or `brew install arduino-cli`) and the esp32 boards.")


CHIPS = {"ESP32-S3": "esp32s3", "ESP32-S2": "esp32s2", "ESP32-C3": "esp32c3", "ESP32-C6": "esp32c6", "ESP32": "esp32"}


def _esptool():
    found = sorted(glob.glob(str(Path.home() / "Library/Arduino15/packages/esp32/tools/esptool_py/*/esptool")))
    return found[-1] if found else shutil.which("esptool") or shutil.which("esptool.py")


def detect_fqbn(port: str) -> str:
    """Ask the chip what it is (ESP32, S3, C3...) so the firmware is built for the right one."""
    tool = _esptool()
    if not tool:
        return FQBN
    out = subprocess.run([tool, "--port", port, "chip-id"], capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"Chip type:\s*(ESP32(?:-[A-Z]\d)?)", out) or re.search(r"Connected to (ESP32(?:-[A-Z]\d)?)", out)
    if not m or m.group(1) not in CHIPS:
        sys.exit("Couldn't identify the board. Hold its BOOT button while plugging it in, then try again.")
    return f"esp32:esp32:{CHIPS[m.group(1)]}"


def _flash(port=None):
    cfg = setup()
    ports = [port] if port else serial_ports()
    if not ports:
        sys.exit("No ESP32 on USB. Plug it in with a data cable (some cables only charge) and try again.")
    if len(ports) > 1:
        sys.exit("More than one serial device: pick one with  python3 sensors.py flash --port /dev/cu.xxx\n  "
                 + "\n  ".join(ports))
    cli = _arduino_cli()
    fqbn = detect_fqbn(ports[0])
    print(f"Found {fqbn.split(':')[-1].upper()} on {ports[0]}.")
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)   # the build holds the key: keep it private
    try:
        print(f"Compiling for {fqbn}...")
        subprocess.run([cli, "compile", "--fqbn", fqbn, "--build-path", str(BUILD_DIR), str(FIRMWARE)], check=True)
        print(f"Uploading to {ports[0]}...")
        subprocess.run([cli, "upload", "--fqbn", fqbn, "--input-dir", str(BUILD_DIR), "-p", ports[0], str(FIRMWARE)],
                       check=True)
    finally:
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    print("Done. Run  python3 sensors.py test  to check it.")
    return cfg


def _test():
    cfg = load_config()
    if cfg is None:
        sys.exit("Run  python3 sensors.py setup  (or flash) first.")
    h = Hub(cfg)
    if not h._connect():
        sys.exit(h.problem)
    print(json.dumps(h.info, indent=2))
    print(json.dumps(h.call("read"), indent=2))
    h.stop()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Jarvis ESP32 sensor hub")
    ap.add_argument("action", choices=["setup", "flash", "test", "ports"])
    ap.add_argument("--port", help="serial port, e.g. /dev/cu.usbserial-0001")
    args = ap.parse_args(argv)
    os.umask(0o077)
    if args.action == "setup":
        setup()
        print(f"Key saved to {CONFIG} and {SECRETS_H} (both private to you).")
    elif args.action == "flash":
        _flash(args.port)
    elif args.action == "test":
        _test()
    else:
        print("\n".join(serial_ports()) or "No USB serial devices found.")


if __name__ == "__main__":
    main()
