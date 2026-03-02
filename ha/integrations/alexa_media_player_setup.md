# Alexa Media Player — HACS Setup

Brings all Echo Dots into HA as `media_player` entities. Required for whole-home audio control.

## Install via HACS

1. Open HA → **HACS** (sidebar)
2. Search for **Alexa Media Player**
3. Install → Restart HA: `docker restart homeassistant`

## Configure

Add to `/home/sysop/homeassistant/configuration.yaml`:

```yaml
alexa_media:
  accounts:
    - email: !secret amazon_email
      password: !secret amazon_password
      url: amazon.com  # or amazon.co.uk, amazon.de, etc.
```

Add to `/home/sysop/homeassistant/secrets.yaml`:

```yaml
amazon_email: mattcarp1@gmail.com  # or whichever Amazon account the Echos are registered to
amazon_password: YOUR_AMAZON_PASSWORD
```

⚠️ **Note:** Amazon uses 2FA — first login will likely send a verification code. HA will show a notification asking for the code. Check HA notifications after restart.

## Expected Entities

After auth, each Echo Dot appears as:
- `media_player.echo_dot_<device_name>` (e.g., `media_player.echo_dot_kitchen`)
- Also creates `notify.alexa_media_*` services for TTS announcements

## TTS Announcements (better than google_translate_say)

With Alexa Media Player, use the native notify service:

```yaml
service: notify.alexa_media_whole_house
data:
  message: "Someone is at the front door."
  data:
    type: announce  # plays announcement even if music is playing
```

Or target specific devices:

```yaml
service: notify.alexa_media_echo_dot_living_room
data:
  message: "Dinner is ready."
```

## Music Control

```yaml
# Play Spotify playlist on all Echo Dots
service: media_player.play_media
target:
  entity_id: media_player.whole_house
data:
  media_content_id: spotify:playlist:YOUR_PLAYLIST_ID
  media_content_type: music
```

## Docs

https://github.com/custom-components/alexa_media_player
