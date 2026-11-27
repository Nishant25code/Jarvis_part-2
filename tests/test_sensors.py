"""ESP32 sensor hub: the signed protocol, end to end against a fake board on a local socket.
Run:  .venv/bin/python -m unittest tests.test_sensors -v"""
import hashlib
import hmac
import json
import secrets
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sensors  # noqa: E402
from tools import sensors as sensor_tools  # noqa: E402

KEY = "11" * 32


class FakeBoard:
    """Does what jarvis_sensors.ino does, written separately so the two must agree."""

    def __init__(self, key_hex=KEY, forge=False):
        self.master = bytes.fromhex(key_hex)
        self.forge = forge
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.relay = False
        self.led = None
        self.conn = None
        self.rejected = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _mac(self, key, d, seq, payload):
        return hmac.new(key, f"{d}|{seq}|{payload}".encode(), hashlib.sha256).hexdigest()[:32]

    def send_event(self, payload):
        self.tx += 1
        key = secrets.token_bytes(32) if self.forge else self.key
        self.conn.sendall(f"{self.tx} {self._mac(key, 'd', self.tx, payload)} {payload}\n".encode())

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn = conn
        f = conn.makefile("r")
        self.rx = self.tx = 0
        self.key = None
        for line in f:
            line = line.strip()
            if line.startswith("hello "):
                cn = line.split()[1]
                dn = secrets.token_hex(16)
                self.key = hmac.new(self.master, f"jarvis-session|{cn}|{dn}".encode(), hashlib.sha256).digest()
                self.rx = self.tx = 0
                conn.sendall(f"hello {dn} jarvis-sensors\n".encode())
                continue
            seq, mac, payload = line.split(" ", 2)
            if int(seq) != self.rx + 1 or not hmac.compare_digest(mac, self._mac(self.key, "c", int(seq), payload)):
                self.rejected += 1
                conn.close()
                return
            self.rx = int(seq)
            rid, cmd, *args = payload.split()
            reply = {"id": int(rid), "ok": True}
            if cmd == "info":
                reply.update(fw="1.0", parts=["temperature", "humidity", "light", "distance", "relay"], wifi="off")
            elif cmd == "read":
                reply.update(temp_c=24.5, humidity=48, light=72, distance_cm=60.2, relay=self.relay)
            elif cmd == "relay":
                self.relay = args[0] == "1"
                reply["relay"] = self.relay
            elif cmd == "led":
                self.led = tuple(map(int, args))
            self.send_event(json.dumps(reply))

    def close(self):
        try:
            if self.conn:
                self.conn.close()
            self.srv.close()
        except OSError:
            pass


def hub_for(board, key=KEY, events=None):
    cfg = {"key": key, "host": "127.0.0.1", "tcp_port": board.port, "relay_name": "desk lamp",
           "status_light": True, "presence_cm": 120}
    sensors.serial_ports = lambda: []          # never touch real USB devices in tests
    h = sensors.Hub(cfg, on_event=(lambda n, d: events.append(n)) if events is not None else None, log=lambda m: None)
    return h


class Protocol(unittest.TestCase):
    def test_seal_and_open_round_trip(self):
        a = sensors.Session(bytes.fromhex(KEY))
        self.assertTrue(a.accept_hello("hello " + "ab" * 16 + " jarvis-sensors"))
        line = a.seal("1 read")
        seq, mac, payload = line.split(" ", 2)
        self.assertEqual((seq, payload), ("1", "1 read"))
        self.assertEqual(len(mac), 32)
        # a line from the board, signed the board's way
        board_mac = hmac.new(a.key, b"d|1|{\"ok\":true}", hashlib.sha256).hexdigest()[:32]
        self.assertEqual(a.open(f"1 {board_mac} " + '{"ok":true}'), '{"ok":true}')

    def test_replay_tamper_and_bad_hello_are_rejected(self):
        a = sensors.Session(bytes.fromhex(KEY))
        self.assertFalse(a.accept_hello("hello not-hex"))
        self.assertIsNone(a.open("1 " + "0" * 32 + " {}"))            # no session yet
        a.accept_hello("hello " + "cd" * 16)
        good = hmac.new(a.key, b"d|1|{}", hashlib.sha256).hexdigest()[:32]
        self.assertIsNone(a.open(f"1 {good} {{\"x\":1}}"))             # payload changed
        self.assertEqual(a.open(f"1 {good} {{}}"), "{}")
        self.assertIsNone(a.open(f"1 {good} {{}}"))                    # replayed
        with self.assertRaises(ValueError):
            a.seal("1 read\n2 relay 1")                                # no smuggled second line

    def test_commands_are_whitelisted_shapes(self):
        h = sensors.Hub({"key": KEY}, log=lambda m: None)
        for bad in ("read; rm", "relay 1 2 3 4", "LED 1", "beep 99999999"):
            with self.assertRaises(ValueError):
                h.call(bad)


class EndToEnd(unittest.TestCase):
    def test_connect_read_switch_and_events(self):
        board = FakeBoard()
        events = []
        h = hub_for(board, events=events)
        try:
            self.assertTrue(h._connect(), h.problem)
            self.assertTrue(h.connected)
            self.assertEqual(h.via, "Wi-Fi")
            sensors.hub = h
            text = sensor_tools.room_sensors()
            self.assertIn("24.5°C", text)
            self.assertIn("someone is at the desk (60 cm away)", text)
            self.assertIn("desk lamp off", text)
            import tools
            tools.user_text = "what's the temperature in here?"
            self.assertIn("Not switched", sensor_tools.switch_relay(True))   # the model's own idea: refused
            self.assertFalse(board.relay)
            for said in ("is the fan on?", "what's the temperature with the lamp off", "jarvis is the lamp on"):
                tools.user_text = said
                self.assertIn("Not switched", sensor_tools.switch_relay(True), said)
            tools.user_text = "turn off the desk lamp"
            self.assertIn("Not switched", sensor_tools.switch_relay(True))     # wrong direction: refused
            self.assertFalse(board.relay)
            tools.user_text = "turn it on"                                   # "it" could be anything
            self.assertIn("Not switched", sensor_tools.switch_relay(True))
            tools.user_text = "could you turn on the desk lamp please?"
            self.assertEqual(sensor_tools.switch_relay(True), "Switched the desk lamp on.")
            self.assertTrue(board.relay)
            sensor_tools.set_status_light("purple")
            self.assertEqual(board.led, (130, 0, 220))
            self.assertIn("Connected over Wi-Fi", sensor_tools.sensor_hub_status())
            board.send_event('{"event":"touch"}')
            for _ in range(50):
                if events:
                    break
                time.sleep(0.02)
            self.assertEqual(events, ["touch"])
        finally:
            sensors.hub = None
            h.stop()
            board.close()

    def test_wrong_key_never_connects(self):
        board = FakeBoard(key_hex="22" * 32)
        h = hub_for(board)
        try:
            self.assertFalse(h._connect())
            self.assertFalse(h.connected)
            self.assertEqual(board.rejected, 1)         # the board refused our signature
        finally:
            h.stop()
            board.close()

    def test_forged_event_drops_the_link(self):
        board = FakeBoard()
        events = []
        h = hub_for(board, events=events)
        try:
            self.assertTrue(h._connect(), h.problem)
            board.forge = True
            board.send_event('{"event":"touch"}')       # someone faking a wake-up tap
            for _ in range(50):
                if not h.connected:
                    break
                time.sleep(0.02)
            self.assertFalse(h.connected)
            self.assertEqual(events, [])
            self.assertIn("wasn't signed", h.problem)
        finally:
            h.stop()
            board.close()


class Describe(unittest.TestCase):
    def test_wording_and_junk_values(self):
        text = sensor_tools.describe({"temp_c": None, "humidity": "lots", "light": 5, "distance_cm": 300,
                                      "soil": 12, "door_open": True, "relay": True}, 120, "fan")
        self.assertIn("Not connected or not answering: temperature/humidity (DHT11)", text)
        self.assertIn("nothing for you to switch or restart", text)
        self.assertNotIn("lots", text)
        self.assertIn("Light 5% (dark)", text)
        self.assertIn("nobody at the desk", text)
        self.assertIn("dry, needs water", text)
        self.assertIn("door open", text)
        self.assertIn("fan on", text)


class Setup(unittest.TestCase):
    def test_setup_keeps_wifi_and_is_private(self):
        import os
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        fw = tmp / "fw"
        fw.mkdir()
        (fw / "secrets.example.h").write_text((sensors.FIRMWARE / "secrets.example.h").read_text())
        (fw / "secrets.h").write_text('#define JARVIS_KEY ""\n#define WIFI_SSID "Home"\n#define WIFI_PASS "pw"\n')
        old = sensors.CONFIG, sensors.FIRMWARE, sensors.SECRETS_H
        sensors.CONFIG, sensors.FIRMWARE, sensors.SECRETS_H = tmp / "j" / "sensors.json", fw, fw / "secrets.h"
        try:
            cfg = sensors.setup()
            again = sensors.setup()
            self.assertEqual(cfg["key"], again["key"])                    # stable across runs
            text = (fw / "secrets.h").read_text()
            self.assertIn(f'"{cfg["key"]}"', text)
            self.assertIn('WIFI_SSID "Home"', text)
            self.assertEqual(os.stat(fw / "secrets.h").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(sensors.CONFIG).st_mode & 0o777, 0o600)
        finally:
            sensors.CONFIG, sensors.FIRMWARE, sensors.SECRETS_H = old


if __name__ == "__main__":
    unittest.main()
