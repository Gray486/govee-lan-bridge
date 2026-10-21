# govee-lan-bridge

Local control of the three light sections of the **Govee H60B0 Uplighter Floor Lamp** from
Home Assistant. It uses **LAN only**: no Govee cloud, no API key, and no internet connection.

Govee's documented LAN API only controls the lamp as a whole: the white down light stays on and
the side ring can't be addressed. This bridge sends the lamp's own section commands over LAN
(`ptReal`) and exposes each section to Home Assistant through MQTT discovery.

## Entities
With `DEVICE_ID=living_room_uplight` you get:

| Entity | Controls |
|---|---|
| `light.living_room_uplight_ripple` | top ("Ripple"): on/off, brightness, RGB, 2700–6500 K |
| `light.living_room_uplight_side` | middle ring ("Side", all 8 segments): on/off, brightness, RGB, 2700–6500 K |
| `light.living_room_uplight_lower` | down light ("Lower"): on/off, brightness, 2700–6500 K |
| `light.living_room_uplight_side_segment_1` … `_8` | each side segment: on/off, brightness, RGB, 2700–6500 K |
| `number.living_room_uplight_flow_rate` | rotation speed of the ripple lens, 0–100 (0 ≈ stopped) |

## Requirements
- An H60B0 with **LAN Control** enabled in the Govee Home app, and a fixed IP (DHCP reservation)
- An MQTT broker with Home Assistant's MQTT integration (discovery prefix `homeassistant`)
- Docker on a host on the same LAN as the lamp (host networking)

## Run
```yaml
services:
  govee-lan-bridge:
    image: swilli23/govee-lan-bridge:latest
    restart: unless-stopped
    network_mode: host
    environment:
      LAMP_IP: 192.0.2.10
      MQTT_HOST: 127.0.0.1
      DEVICE_ID: living_room_uplight
      DEVICE_NAME: Living Room Uplight
```

| Variable | Default | |
|---|---|---|
| `LAMP_IP` | *(required)* | Lamp's LAN IP |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | Broker |
| `MQTT_USER` / `MQTT_PASS` | *(none)* | Broker credentials, if required |
| `DEVICE_ID` / `DEVICE_NAME` | `govee_h60b0` / `Govee H60B0` | Entity ID prefix / device name |
| `DISCOVERY_PREFIX` | `homeassistant` | MQTT discovery prefix |
| `BATCH_WINDOW` | `0.15` | Seconds within which commands are merged into one change |
| `MAX_FRAMES_PER_PACKET` | `12` | Commands per LAN packet before splitting |

## Behaviour
- **The lamp allows two sections on at once.** Turning on a third switches off the section that's been
  on longest, and Home Assistant is updated to match.
- **Scenes apply atomically.** Commands arriving within 150 ms (`BATCH_WINDOW`) are merged into one change:
  turn-offs first, then turn-ons, then the two-section rule. A scene moving between section pairs lands
  exactly as defined, whatever order Home Assistant sends the commands in.
- **One packet per change.** Separate LAN packets sent back-to-back get dropped by the lamp, so every
  change (e.g. a scene setting colour and brightness) is sent as a single packet.
- **Side segments.** The `side` light controls the whole ring and repaints all 8 segments; the segment
  lights control one each. Turning a segment on lights the side section (two-section rule applies);
  turning the last lit segment off switches the section off. A segment's "off" is minimum brightness,
  since the lamp has no per-segment off. Segments sharing a setting are sent as one frame with a merged
  mask, and the section comes back with its segment pattern intact.
- **Packet size.** Up to 12 commands per packet (`MAX_FRAMES_PER_PACKET`); larger changes are split
  into consecutive packets 50 ms apart.
- **Safety:** colour temperatures are clamped to 2700–6500 K, and the down light never receives colour
  bytes. Either mistake blacks out the lamp until a valid setting is sent.
- **State is "last set"**. The lamp can't report per-section state over LAN, so changes made in the
  Govee app aren't reflected. State is retained in MQTT across restarts.
- **Recovery:** if the lamp ever goes dark from an invalid setting, set a valid colour temperature on it
  (e.g. with govee2mqtt or the Govee app).

## Protocol notes
Commands are standard 20-byte Govee frames (data, zero padding, XOR checksum), sent base64-encoded in a
LAN `ptReal` message to UDP port 4003. Several frames can go in one message's `command` array.

| Function | Frame data |
|---|---|
| Whole lamp on/off | `33 01 01` / `33 01 00` |
| Master brightness | `33 04 <1-100>` |
| Section on/off (1 Ripple, 2 Side, 3 Lower) | `33 30 <sec> 01` / `33 30 <sec> 00` |
| Ripple colour / temperature | `33 05 2c 01 01 RR GG BB KK KK` (Kelvin big-endian, `00 00` for plain colour) |
| Ripple brightness | `33 05 2c 01 02 <1-100>` |
| Ripple flow rate | `33 05 2c 01 03 <0-100>` |
| Side colour / temperature | `33 05 2c 02 01 RR GG BB KK KK <segment mask>` |
| Side brightness | `33 05 2c 02 02 <1-100> <segment mask>` (mask: bit n = segment n+1, `ff` = all) |
| Lower temperature | `33 05 2c 03 01 KK KK` (**no** RGB bytes) |
| Lower brightness | `33 05 2c 03 02 <1-100>` |

These were derived from the Govee Home app's Bluetooth traffic and verified on real hardware over LAN.

## Tests
```
python test_bridge.py
```
Checks every frame byte-for-byte against the app's commands, plus the safety rules and the
two-section logic. It runs offline, and the GitHub Action runs it before publishing an image.

## Publishing
On every push to `main`, the GitHub Action runs the tests and publishes `latest` (amd64 + arm64) to
Docker Hub. Tags like `v1.2.0` also publish `1.2.0` and `1.2`. It needs the repository secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with write scope).
