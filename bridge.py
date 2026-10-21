"""Govee LAN bridge: exposes the Govee H60B0 uplighter floor lamp's three sections to Home Assistant over MQTT.

LAN-only. Commands go to the lamp as Govee LAN `ptReal` frames (UDP 4003); Home Assistant
talks to this bridge through MQTT discovery. Standard library only, so nothing is
downloaded at runtime.

Entities created in Home Assistant:
  light.<name>_ripple   top section: on/off, brightness, RGB, colour temperature
  light.<name>_side     middle ring (all 8 segments): on/off, brightness, RGB, colour temperature
  light.<name>_lower    down light: on/off, brightness, colour temperature
  number.<name>_flow_rate  ripple lens rotation speed 0-100

The lamp cannot report per-section state over LAN, so state is what the bridge last set
(retained in MQTT so it survives restarts).
"""

import base64
import json
import math
import os
import socket
import struct
import threading
import time

LAMP_IP = os.environ.get("LAMP_IP", "")
LAMP_PORT = 4003
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
DEVICE_ID = os.environ.get("DEVICE_ID", "govee_h60b0")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Govee H60B0")
BASE = f"govee_lan/{DEVICE_ID}"
DISCOVERY = os.environ.get("DISCOVERY_PREFIX", "homeassistant")

MIN_K, MAX_K = 2700, 6500          # outside this range the lamp blacks out
MIN_GAP = 0.05                     # seconds between UDP datagrams to the lamp
SECTIONS = {"ripple": 1, "side": 2, "lower": 3}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- lamp protocol

def frame(*data):
    """20-byte Govee frame: data, zero padding, XOR checksum."""
    b = list(data) + [0] * (19 - len(data))
    x = 0
    for v in b:
        x ^= v
    return bytes(b + [x])


def kelvin_to_rgb(k):
    """Approximate RGB for a colour temperature (the app sends one alongside the Kelvin value)."""
    t = k / 100.0
    if t <= 66:
        r = 255
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255
    return tuple(max(0, min(255, int(c))) for c in (r, g, b))


def clamp_k(k):
    return max(MIN_K, min(MAX_K, int(k)))


def pct(brightness_255):
    return max(1, min(100, round(int(brightness_255) * 100 / 255)))


def f_power(on):            return frame(0x33, 0x01, 1 if on else 0)
def f_section(sec, on):     return frame(0x33, 0x30, sec, 1 if on else 0)
def f_flow(v):              return frame(0x33, 0x05, 0x2C, 0x01, 0x03, max(0, min(100, int(v))))


def f_brightness(sec, p):
    if sec == 2:
        return frame(0x33, 0x05, 0x2C, 2, 0x02, p, 0xFF)   # side needs the segment mask
    return frame(0x33, 0x05, 0x2C, sec, 0x02, p)


def f_rgb(sec, r, g, b):
    if sec == 3:
        raise ValueError("lower light has no colour")       # colour bytes black out the lower light
    extra = (0xFF,) if sec == 2 else ()
    return frame(0x33, 0x05, 0x2C, sec, 0x01, r, g, b, 0, 0, *extra)


def f_kelvin(sec, k):
    k = clamp_k(k)
    if sec == 3:
        return frame(0x33, 0x05, 0x2C, 3, 0x01, k >> 8, k & 0xFF)   # no RGB bytes for lower
    r, g, b = kelvin_to_rgb(k)
    extra = (0xFF,) if sec == 2 else ()
    return frame(0x33, 0x05, 0x2C, sec, 0x01, r, g, b, k >> 8, k & 0xFF, *extra)


class Lamp:
    """Sends batches of frames as single ptReal datagrams, spaced at least MIN_GAP apart."""

    def __init__(self, ip):
        self.addr = (ip, LAMP_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()
        self.last = 0.0

    def send(self, frames):
        if not frames:
            return
        msg = {"msg": {"cmd": "ptReal", "data": {"command": [base64.b64encode(f).decode() for f in frames]}}}
        with self.lock:
            wait = self.last + MIN_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.sock.sendto(json.dumps(msg).encode(), self.addr)
            self.last = time.monotonic()
        log(f"-> lamp: {' | '.join(f.hex(' ') for f in frames)}")


# ---------------------------------------------------------------- minimal MQTT 3.1.1 client

def _str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _remaining(n):
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MQTT:
    def __init__(self, on_message):
        self.on_message = on_message
        self.sock = None
        self.wlock = threading.Lock()
        self.pid = 0

    def _send(self, ptype, body):
        with self.wlock:
            self.sock.sendall(bytes([ptype]) + _remaining(len(body)) + body)

    def connect(self, will_topic, will_msg):
        self.sock = socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=10)
        flags = 0x02 | 0x04 | 0x20          # clean session, will flag, will retain (QoS 0)
        payload = _str(f"govee-lan-bridge-{DEVICE_ID}") + _str(will_topic) + _str(will_msg)
        if MQTT_USER:
            flags |= 0x80
            payload += _str(MQTT_USER)
            if MQTT_PASS:
                flags |= 0x40
                payload += _str(MQTT_PASS)
        self._send(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60) + payload)
        ptype, body = self._read()
        if ptype != 0x20 or body[1] != 0:
            raise ConnectionError(f"MQTT connect refused (code {body[1] if len(body) > 1 else '?'})")
        self.sock.settimeout(None)

    def subscribe(self, topic):
        self.pid = self.pid % 65535 + 1
        self._send(0x82, struct.pack("!H", self.pid) + _str(topic) + b"\x00")

    def publish(self, topic, payload, retain=False):
        if not isinstance(payload, (bytes, bytearray)):
            payload = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self._send(0x30 | (1 if retain else 0), _str(topic) + payload)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("MQTT connection closed")
            buf += chunk
        return buf

    def _read(self):
        ptype = self._recv_exact(1)[0]
        mult, length = 1, 0
        while True:
            d = self._recv_exact(1)[0]
            length += (d & 0x7F) * mult
            mult *= 128
            if not d & 0x80:
                break
        return ptype, self._recv_exact(length) if length else b""

    def loop(self):
        """Read packets until the connection drops; a helper thread sends keep-alive pings."""
        stop = threading.Event()

        def pinger():
            while not stop.wait(30):
                try:
                    self._send(0xC0, b"")
                except OSError:
                    return
        threading.Thread(target=pinger, daemon=True).start()
        try:
            while True:
                ptype, body = self._read()
                if ptype >> 4 == 3:                       # PUBLISH
                    tlen = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    off = 2 + tlen + (2 if (ptype >> 1) & 3 else 0)
                    try:
                        self.on_message(topic, body[off:], bool(ptype & 1))
                    except Exception as e:                # never let one bad message kill the bridge
                        log(f"error handling {topic}: {e!r}")
        finally:
            stop.set()
            try:
                self.sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------- bridge logic

class Bridge:
    def __init__(self):
        self.lamp = Lamp(LAMP_IP)
        self.mqtt = MQTT(self.handle)
        # Last-set state per section (restored from retained MQTT state at startup).
        self.state = {
            "ripple": {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000, "color": {"r": 255, "g": 200, "b": 150}},
            "side":   {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000, "color": {"r": 255, "g": 200, "b": 150}},
            "lower":  {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000},
        }
        self.flow = 20
        self.on_order = []            # sections in the order they were turned on (lamp allows two)
        self.restoring = True

    # -- discovery --------------------------------------------------------
    def device(self):
        return {"identifiers": [f"govee_lan_{DEVICE_ID}"], "name": DEVICE_NAME,
                "manufacturer": "Govee", "model": "H60B0 (LAN bridge)"}

    def discover(self):
        avail = {"availability_topic": f"{BASE}/status"}
        for name in SECTIONS:
            modes = ["color_temp"] if name == "lower" else ["rgb", "color_temp"]
            cfg = {"name": name.capitalize(), "unique_id": f"{DEVICE_ID}_{name}",
                   "object_id": f"{DEVICE_ID}_{name}", "schema": "json",
                   "command_topic": f"{BASE}/{name}/set", "state_topic": f"{BASE}/{name}/state",
                   "brightness": True, "supported_color_modes": modes, "color_temp_kelvin": True,
                   "min_kelvin": MIN_K, "max_kelvin": MAX_K, "device": self.device(), **avail}
            self.mqtt.publish(f"{DISCOVERY}/light/{DEVICE_ID}/{name}/config", cfg, retain=True)
        num = {"name": "Flow rate", "unique_id": f"{DEVICE_ID}_flow_rate", "object_id": f"{DEVICE_ID}_flow_rate",
               "command_topic": f"{BASE}/flow/set", "state_topic": f"{BASE}/flow/state",
               "min": 0, "max": 100, "step": 1, "mode": "slider", "icon": "mdi:rotate-3d-variant",
               "device": self.device(), **avail}
        self.mqtt.publish(f"{DISCOVERY}/number/{DEVICE_ID}/flow_rate/config", num, retain=True)

    def publish_state(self, name):
        s = self.state[name]
        out = {"state": s["state"], "brightness": s["brightness"], "color_mode": s["color_mode"]}
        if s["color_mode"] == "color_temp":
            out["color_temp"] = s["color_temp"]
        else:
            out["color"] = s["color"]
        self.mqtt.publish(f"{BASE}/{name}/state", out, retain=True)

    # -- commands ------------------------------------------------------------
    def section_frames(self, name, cmd):
        """Frames for one section's command; updates self.state."""
        sec, s, frames = SECTIONS[name], self.state[name], []
        if cmd.get("state") == "OFF":
            s["state"] = "OFF"
            if name in self.on_order:
                self.on_order.remove(name)
            return [f_section(sec, False)]
        turning_on = s["state"] != "ON"
        s["state"] = "ON"
        if turning_on:
            frames += self.make_room(name)
            frames += [f_power(True), f_section(sec, True)]
        if name in self.on_order:
            self.on_order.remove(name)
        self.on_order.append(name)
        if "color" in cmd and name != "lower":
            c = cmd["color"]
            s["color"], s["color_mode"] = {k: int(c[k]) for k in "rgb"}, "rgb"
            frames.append(f_rgb(sec, *(s["color"][k] for k in "rgb")))
        elif "color_temp" in cmd:
            s["color_temp"], s["color_mode"] = clamp_k(cmd["color_temp"]), "color_temp"
            frames.append(f_kelvin(sec, s["color_temp"]))
        elif turning_on:              # re-apply remembered colour so the section comes back as it was
            frames.append(f_kelvin(sec, s["color_temp"]) if s["color_mode"] == "color_temp"
                          else f_rgb(sec, *(s["color"][k] for k in "rgb")))
        if "brightness" in cmd:
            s["brightness"] = int(cmd["brightness"])
            frames.append(f_brightness(sec, pct(s["brightness"])))
        elif turning_on:
            frames.append(f_brightness(sec, pct(s["brightness"])))
        return frames

    def make_room(self, name):
        """The lamp allows two sections at once: switch off the longest-on other section."""
        on = [n for n in self.on_order if n != name and self.state[n]["state"] == "ON"]
        frames = []
        while len(on) >= 2:
            victim = on.pop(0)
            self.state[victim]["state"] = "OFF"
            self.on_order.remove(victim)
            frames.append(f_section(SECTIONS[victim], False))
            self.publish_state(victim)
            log(f"two-section limit: turned {victim} off")
        return frames

    def handle(self, topic, payload, retained):
        parts = topic.split("/")
        # Retained state topics: restore memory at startup only, never drive the lamp.
        if parts[-1] == "state":
            if self.restoring and retained:
                if parts[-2] in self.state:
                    self.state[parts[-2]].update(json.loads(payload))
                    if self.state[parts[-2]]["state"] == "ON" and parts[-2] not in self.on_order:
                        self.on_order.append(parts[-2])
                elif parts[-2] == "flow":
                    self.flow = int(float(payload))
            return
        if parts[-1] != "set":
            return
        target = parts[-2]
        if target == "flow":
            self.flow = max(0, min(100, int(float(payload))))
            self.lamp.send([f_flow(self.flow)])
            self.mqtt.publish(f"{BASE}/flow/state", str(self.flow), retain=True)
        elif target in SECTIONS:
            frames = self.section_frames(target, json.loads(payload))
            self.lamp.send(frames)                       # one datagram for the whole change
            self.publish_state(target)

    # -- main loop ----------------------------------------------------------------
    def run(self):
        backoff = 2
        while True:
            try:
                self.mqtt.connect(f"{BASE}/status", "offline")
                log(f"connected to MQTT {MQTT_HOST}:{MQTT_PORT}; lamp {LAMP_IP}")
                self.restoring = True
                self.discover()
                for name in SECTIONS:
                    self.mqtt.subscribe(f"{BASE}/{name}/state")
                self.mqtt.subscribe(f"{BASE}/flow/state")
                threading.Timer(2.0, self.finish_restore).start()
                self.mqtt.publish(f"{BASE}/status", "online", retain=True)
                backoff = 2
                self.mqtt.loop()
            except Exception as e:
                log(f"MQTT error: {e!r}; reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def finish_restore(self):
        """After retained state has arrived, start listening for commands and republish state."""
        self.restoring = False
        for name in SECTIONS:
            self.mqtt.subscribe(f"{BASE}/{name}/set")
            self.publish_state(name)
        self.mqtt.subscribe(f"{BASE}/flow/set")
        self.mqtt.publish(f"{BASE}/flow/state", str(self.flow), retain=True)
        log(f"ready; restored state: { {n: s['state'] for n, s in self.state.items()} }, flow {self.flow}")


if __name__ == "__main__":
    if not LAMP_IP:
        raise SystemExit("LAMP_IP is not set: give the lamp's LAN IP address (and a DHCP reservation)")
    Bridge().run()
