# Claudette Home — HA Configuration

Home Assistant configuration files and guides for the Xagħra prototype.

## Architecture

```
Claudette (brain)
    ↓
Home Assistant (HA) — localhost:8123, Docker
    ↓
├── WiiM streamer (media_player.wiim_mini) — hi-fi audio source
├── 15× Echo Dots (media_player.echo_dot_*) — whole-home speakers
├── Eufy 340 doorbell (binary_sensor.eufy_doorbell_button) — entry detection
└── Aqara Hub M3 (arriving ~5 Mar) — Zigbee/Thread/Matter coordinator
```

## Setup Order

1. **WiiM Integration** → `integrations/wiim_setup.md`
   Status: ⏳ Pending (needs Mattie to run in HA UI)

2. **Alexa Media Player (HACS)** → `integrations/alexa_media_player_setup.md`
   Status: ⏳ Pending (needs Amazon auth)

3. **Media Player Groups** → `integrations/media_player_groups.yaml`
   Status: ⏳ Pending (update entity IDs after steps 1 & 2)

4. **Doorbell Automation** → `automations/doorbell_announcement.yaml`
   Status: ⏳ Pending (update entity IDs after Eufy confirmed in HA)

5. **Aqara M3 Hub** — arriving 5–12 March
   Unblocks: Zigbee devices, Thread/Matter network, sensors

## Whole-Home Audio Flow

```
"Claudette, play jazz"
        ↓
Intent Parser → HA media_player.play_media
        ↓
WiiM + all Echo Dots (media_player.whole_house group)

Doorbell press
        ↓
HA automation → duck volume → TTS announce → restore volume
```

## Key Entity Names (update after discovery)

| Device | Expected Entity ID |
|--------|-------------------|
| WiiM streamer | `media_player.wiim_mini` |
| All Echo Dots | `media_player.whole_house` (group) |
| Eufy doorbell button | `binary_sensor.eufy_doorbell_t8200_button` |
| Eufy doorbell camera | `camera.eufy_doorbell_t8200` |
| Fire TV | `media_player.fire_tv` |

## HA Location

- **Container:** `homeassistant` (Docker)
- **Config:** `/home/sysop/homeassistant/`
- **URL:** `http://localhost:8123` (or `http://100.118.26.94:8123` via Tailscale)
- **Restart:** `docker restart homeassistant`
