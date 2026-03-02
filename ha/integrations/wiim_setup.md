# WiiM Integration Setup

WiiM has a native Home Assistant integration (no HACS needed).

## Setup (HA UI)

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **WiiM**
3. HA will auto-discover your WiiM on the local network (mDNS)
4. If not auto-discovered: enter the WiiM's local IP address manually

## Expected Entities

After setup, HA creates:
- `media_player.wiim_mini` (or `wiim_amp`, `wiim_pro` — depends on model)
- Attributes: volume, source, media title, artist, album

## Supported Features

- ✅ Play / Pause / Stop
- ✅ Volume control
- ✅ Source selection (Spotify, AirPlay, Bluetooth, optical, etc.)
- ✅ Media info (now playing)
- ✅ Group playback (WiiM Link)

## Add to Whole-House Group

After discovery, note the entity ID (e.g., `media_player.wiim_mini`) and add to:
`ha/integrations/media_player_groups.yaml`

## Docs

https://www.home-assistant.io/integrations/wiim/
