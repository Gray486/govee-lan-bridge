"""Offline tests: frames must match what the Govee app sent in the BLE captures,
and the bridge logic must respect the lamp's rules. Run: python test_bridge.py"""

import json
import os
os.environ.setdefault("LAMP_IP", "192.0.2.1")   # TEST-NET address, never contacted
import bridge as B


def hexf(f):
    return f.hex(" ")


# Frames byte-for-byte against the decrypted app captures.
CAPTURED = {
    "lower off":             (B.f_section(3, False), "33 30 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"),
    "ripple on":             (B.f_section(1, True),  "33 30 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 03"),
    "lower 2700K":           (B.f_kelvin(3, 2700),   "33 05 2c 03 01 0a 8c 00 00 00 00 00 00 00 00 00 00 00 00 9e"),
    "lower 6500K":           (B.f_kelvin(3, 6500),   "33 05 2c 03 01 19 64 00 00 00 00 00 00 00 00 00 00 00 00 65"),
    "lower brightness 1%":   (B.f_brightness(3, 1),  "33 05 2c 03 02 01 00 00 00 00 00 00 00 00 00 00 00 00 00 1a"),
    "ripple brightness 100": (B.f_brightness(1, 100), "33 05 2c 01 02 64 00 00 00 00 00 00 00 00 00 00 00 00 00 7d"),
    "flow 55":               (B.f_flow(55),          "33 05 2c 01 03 37 00 00 00 00 00 00 00 00 00 00 00 00 00 2f"),
    "ripple rgb 00ffff":     (B.f_rgb(1, 0, 255, 255), "33 05 2c 01 01 00 ff ff 00 00 00 00 00 00 00 00 00 00 00 1a"),
    "master power off":      (B.frame(0x33, 0x01, 0), "33 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 32"),
}
fails = 0
for name, (got, want) in CAPTURED.items():
    ok = hexf(got) == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:24} {hexf(got)}" + ("" if ok else f"\n      want {want}"))

# Safety rules.
assert B.f_kelvin(3, 9000)[5:7] == bytes([0x19, 0x64]), "kelvin must clamp to 6500"
assert B.f_kelvin(3, 1000)[5:7] == bytes([0x0a, 0x8c]), "kelvin must clamp to 2700"
try:
    B.f_rgb(3, 255, 0, 0)
    raise AssertionError("lower light must refuse RGB")
except ValueError:
    pass
assert B.f_brightness(2, 50)[6] == 0xFF, "side brightness needs the segment mask"
print("PASS  safety rules (clamp, no RGB on lower, side mask)")


# Logic with fake MQTT and lamp.
class FakeLamp:
    def __init__(self): self.sent = []
    def send(self, frames): self.sent.append([hexf(f) for f in frames])


class FakeMQTT:
    def __init__(self): self.pub = []
    def publish(self, t, p, retain=False): self.pub.append((t, p))


br = B.Bridge()
br.lamp, br.mqtt = FakeLamp(), FakeMQTT()
br.restoring = False
def cmd(sec, **kw):
    br.handle(f"{B.BASE}/{sec}/set", json.dumps(kw).encode(), False)
    br.timer.cancel(); br.flush()

cmd("ripple", state="ON", color={"r": 255, "g": 0, "b": 0}, brightness=128)
cmd("side", state="ON", color_temp=3000)
assert len(br.lamp.sent) == 2 and all(len(b) > 1 for b in br.lamp.sent), "each change must be ONE datagram"
cmd("lower", state="ON")                         # third section -> ripple (oldest) must go off first
batch = br.lamp.sent[-1]
assert batch[0].startswith("33 30 01 00"), f"ripple should be switched off first, got {batch}"
assert br.state["ripple"]["state"] == "OFF" and br.state["lower"]["state"] == "ON"
print("PASS  two-section limit: turning on lower switched ripple off in the same datagram")
n = len(br.lamp.sent)
cmd("lower", color={"r": 1, "g": 2, "b": 3})     # colour on lower is ignored, not sent
assert not br.lamp.sent[n:] or br.lamp.sent[-1] == [], f"colour must never reach the lower light, got {br.lamp.sent[-1]}"
print("PASS  colour commands to the lower light are dropped")
cmd("side", state="OFF")
assert len(br.lamp.sent[-1]) == 1 and br.lamp.sent[-1][0].startswith("33 30 02 00"), br.lamp.sent[-1]
print("PASS  off is a single section-off frame")
br.handle(f"{B.BASE}/flow/set", b"75", False)
br.timer.cancel(); br.flush()
assert br.lamp.sent[-1][0].startswith("33 05 2c 01 03 4b")
print("PASS  flow rate")

# Scene race: side+lower on; a scene sends ripple ON, side ON, lower OFF in any order within the window.
import itertools
for order in itertools.permutations([("ripple", {"state": "ON", "color": {"r": 9, "g": 9, "b": 9}}),
                                     ("side", {"state": "ON", "brightness": 100}),
                                     ("lower", {"state": "OFF"})]):
    br.state["ripple"]["state"], br.state["side"]["state"], br.state["lower"]["state"] = "OFF", "ON", "ON"
    br.on_order = ["side", "lower"]
    for name, c in order:
        br.handle(f"{B.BASE}/{name}/set", json.dumps(c).encode(), False)
    br.timer.cancel(); br.flush()
    got = {n: br.state[n]["state"] for n in B.SECTIONS}
    assert got == {"ripple": "ON", "side": "ON", "lower": "OFF"}, (order, got)
    assert not any(f.startswith("33 30 02 00") for f in br.lamp.sent[-1]), "side must not be switched off"
    assert len(br.lamp.sent[-1]) > 1
print("PASS  scene race: every command order lands as defined, in one datagram")


# ---- side segments (v1.2)
def fresh(side_on=True):
    b = B.Bridge()
    b.lamp, b.mqtt, b.restoring = FakeLamp(), FakeMQTT(), False
    if side_on:
        b.state["side"]["state"], b.on_order = "ON", ["side"]
    return b


def seg(b, i, **kw):
    b.handle(f"{B.BASE}/side_segment_{i}/set", json.dumps(kw).encode(), False)


def go(b):
    b.timer.cancel()
    b.flush()
    return b.lamp.sent[-1]


# Frames match the live-verified segment commands (segment 1 blue, segment 5 green).
assert hexf(B.f_rgb(2, 0, 0, 255, mask=0x01)).startswith("33 05 2c 02 01 00 00 ff 00 00 01")
assert hexf(B.f_rgb(2, 0, 255, 0, mask=0x10)).startswith("33 05 2c 02 01 00 ff 00 00 00 10")
print("PASS  segment frames match the live-verified commands")

b = fresh()
for i in range(1, 9):
    seg(b, i, state="ON", color={"r": 255, "g": 0, "b": 0} if i % 2 else {"r": 0, "g": 0, "b": 255}, brightness=255)
sent = go(b)
colour = [f for f in sent if f.startswith("33 05 2c 02 01")]
assert len(colour) == 2, f"8 segments in 2 colours must be 2 frames, got {sent}"
assert len(b.lamp.sent) == 1, "one datagram"
print("PASS  8 segments in 2 colours -> 2 colour frames (merged masks), one datagram")

b = fresh()
for i in range(1, 9):
    seg(b, i, state="ON", brightness=3 if i % 2 else 255)    # alternating dim/bright like the live test
sent = go(b)
bri = sorted(f[:20] for f in sent if f.startswith("33 05 2c 02 02"))
assert bri == ["33 05 2c 02 02 01 55", "33 05 2c 02 02 64 aa"], bri
print("PASS  alternating 1%/100% -> masks 0x55 / 0xaa, exactly as verified on the lamp")

b = fresh(side_on=False)
b.state["ripple"]["state"] = b.state["lower"]["state"] = "ON"
b.on_order = ["ripple", "lower"]
seg(b, 3, state="ON", color={"r": 0, "g": 255, "b": 0})
sent = go(b)
assert b.state["side"]["state"] == "ON" and b.state["ripple"]["state"] == "OFF", "segment must light side, evicting oldest"
assert any(f.startswith("33 30 02 01") for f in sent) and any(f.startswith("33 30 01 00") for f in sent)
print("PASS  segment command with side off: side turns on, oldest section makes room")

b = fresh()
for i in range(1, 9):
    seg(b, i, state="OFF")
sent = go(b)
assert b.state["side"]["state"] == "OFF" and any(f.startswith("33 30 02 00") for f in sent)
print("PASS  last lit segment off -> side section off")

b = fresh()
b.segs[2]["color"], b.segs[2]["color_mode"] = {"r": 1, "g": 2, "b": 3}, "rgb"
b.handle(f"{B.BASE}/side/set", json.dumps({"state": "ON", "color": {"r": 9, "g": 8, "b": 7}}).encode(), False)
go(b)
assert all(s["color"] == {"r": 9, "g": 8, "b": 7} and s["color_mode"] == "rgb" for s in b.segs)
assert len([t for t, _ in b.mqtt.pub if "side_segment_" in t]) == 8, "all 8 segment states republished"
print("PASS  whole-ring colour repaints every segment and updates all 8 segment entities")

b = fresh()
b.segs[0]["color"], b.segs[0]["color_mode"] = {"r": 255, "g": 0, "b": 0}, "rgb"
b.handle(f"{B.BASE}/side/set", b'{"state": "OFF"}', False)
go(b)
b.handle(f"{B.BASE}/side/set", b'{"state": "ON"}', False)
sent = go(b)
assert any(f.startswith("33 05 2c 02 01 ff 00 00 00 00 01") for f in sent), "pattern restored on section on"
print("PASS  side off/on restores the segment pattern")

print("\nALL TESTS PASSED" if not fails else f"\n{fails} FRAME MISMATCHES")
