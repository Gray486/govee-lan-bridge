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
    def send(self, frames): self.sent.append([hexf(f)[:17] for f in frames])


class FakeMQTT:
    def __init__(self): self.pub = []
    def publish(self, t, p, retain=False): self.pub.append((t, p))


br = B.Bridge()
br.lamp, br.mqtt = FakeLamp(), FakeMQTT()
br.restoring = False
cmd = lambda sec, **kw: br.handle(f"{B.BASE}/{sec}/set", json.dumps(kw).encode(), False)

cmd("ripple", state="ON", color={"r": 255, "g": 0, "b": 0}, brightness=128)
cmd("side", state="ON", color_temp=3000)
assert len(br.lamp.sent) == 2 and all(len(b) > 1 for b in br.lamp.sent), "each change must be ONE datagram"
cmd("lower", state="ON")                         # third section -> ripple (oldest) must go off first
batch = br.lamp.sent[-1]
assert batch[0].startswith("33 30 01 00"), f"ripple should be switched off first, got {batch}"
assert br.state["ripple"]["state"] == "OFF" and br.state["lower"]["state"] == "ON"
print("PASS  two-section limit: turning on lower switched ripple off in the same datagram")
cmd("lower", color={"r": 1, "g": 2, "b": 3})     # colour on lower is ignored, not sent
assert br.lamp.sent[-1] == [], f"colour must never reach the lower light, got {br.lamp.sent[-1]}"
print("PASS  colour commands to the lower light are dropped")
cmd("side", state="OFF")
assert br.lamp.sent[-1] == ["33 30 02 00 00 00"], br.lamp.sent[-1]
print("PASS  off is a single section-off frame")
br.handle(f"{B.BASE}/flow/set", b"75", False)
assert br.lamp.sent[-1][0].startswith("33 05 2c 01 03 4b")
print("PASS  flow rate")
print("\nALL TESTS PASSED" if not fails else f"\n{fails} FRAME MISMATCHES")
