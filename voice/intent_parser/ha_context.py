#!/usr/bin/env python3
"""
Claudette Home — HA Context Builder
Builds the system prompt context from a Home Assistant entity/scene list.

The intent parser injects this into Claude so it knows what devices exist
and what actions are possible — no hardcoding, fully dynamic.

2026-04-02: Updated REAL_ENTITIES to match actual HA device registry
            (read from /home/sysop/homeassistant/.storage/).
            SAMPLE_ENTITIES preserved as fallback for tests.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# REAL entities — auto-synced from live HA device/entity registry 2026-04-04
# Source: HA .storage (/home/sysop/homeassistant/.storage)
# Re-run: python3 voice/intent_parser/refresh_entities.py [--api] [--write]
# ---------------------------------------------------------------------------
REAL_ENTITIES = {
    "switches": [
        {"entity_id": "switch.9424b87a6361_fresh_air", "name": "AC Fresh Air", "area": "sitting_room"},
        {"entity_id": "switch.9424b87a6361_health_mode", "name": "AC Health Mode", "area": "sitting_room"},
        {"entity_id": "switch.9424b87a6361_panel_light", "name": "AC Panel Light", "area": "sitting_room"},
        {"entity_id": "switch.9424b87a6361_quiet_mode", "name": "AC Quiet Mode", "area": "sitting_room"},
        {"entity_id": "switch.9424b87a6361_xtra_fan", "name": "AC Extra Fan", "area": "sitting_room"},
        {"entity_id": "switch.xaghra_sitting_room_crossfade", "name": "Sonos Crossfade", "area": "sitting_room"},
        {"entity_id": "switch.xaghra_sitting_room_loudness", "name": "Sonos Loudness", "area": "sitting_room"},
        {"entity_id": "switch.xaghra_sitting_room_night_sound", "name": "Sonos Night Mode", "area": "sitting_room"},
        {"entity_id": "switch.xaghra_sitting_room_speech_enhancement", "name": "Sonos Speech Enhancement", "area": "sitting_room"},
        {"entity_id": "switch.xaghra_sitting_room_status_light", "name": "Sonos Status Light", "area": "sitting_room"},
        {"entity_id": "switch.xaghra_sitting_room_subwoofer_enabled", "name": "Subwoofer enabled", "area": "unknown"},
        {"entity_id": "switch.xaghra_sitting_room_surround_enabled", "name": "Surround enabled", "area": "unknown"},
        {"entity_id": "switch.xaghra_sitting_room_surround_music_full_volume", "name": "Surround music full volume", "area": "unknown"},
        {"entity_id": "switch.xaghra_sitting_room_touch_controls", "name": "Touch controls", "area": "unknown"},
    ],
    "media_players": [
        {"entity_id": "media_player.bedroom_tv", "name": "Bedroom TV", "area": "bedroom", "description": "Sony BRAVIA bedroom TV"},
        {"entity_id": "media_player.living_room_tv", "name": "Living Room TV", "area": "living_room", "description": "Sony BRAVIA 4K main TV"},
        {"entity_id": "media_player.xaghra_sittiing_room_wiim", "name": "WiiM Ultra (Sitting Room)", "area": "sitting_room", "description": "WiiM Ultra streamer"},
        {"entity_id": "media_player.xaghra_sitting_room", "name": "Sonos Arc (Sitting Room)", "area": "sitting_room", "description": "Sonos Arc soundbar"},
    ],
    "climate": [
        {"entity_id": "climate.9424b87a6361", "name": "Air Conditioner (Sitting Room)", "area": "sitting_room", "description": "Gree split AC unit"},
    ],
    "sensors": [
        {"entity_id": "weather.forecast_home", "name": "Home Weather", "area": "outdoor", "description": "MET weather forecast"},
        {"entity_id": "binary_sensor.xaghra_sitting_room_microphone", "name": "Microphone", "area": "unknown"},
        {"entity_id": "sensor.xaghra_sitting_room_audio_input_format", "name": "Audio input format", "area": "unknown"},
    ],
}

# ---------------------------------------------------------------------------
# SAMPLE entities — stub data for development/unit tests (no live HA needed)
# Preserved unchanged from original implementation for test compatibility.
# ---------------------------------------------------------------------------
SAMPLE_ENTITIES = {
    "lights": [
        {"entity_id": "light.living_room", "name": "Living Room Light", "area": "living_room"},
        {"entity_id": "light.bedroom", "name": "Bedroom Light", "area": "bedroom"},
        {"entity_id": "light.kitchen", "name": "Kitchen Light", "area": "kitchen"},
        {"entity_id": "light.dining_room", "name": "Dining Room Light", "area": "dining_room"},
        {"entity_id": "light.garden", "name": "Garden Light", "area": "outdoor"},
        {"entity_id": "light.hallway", "name": "Hallway Light", "area": "hallway"},
    ],
    "switches": [
        {"entity_id": "switch.tv", "name": "TV", "area": "living_room"},
        {"entity_id": "switch.air_conditioner", "name": "Air Conditioner", "area": "living_room"},
        {"entity_id": "switch.kettle", "name": "Kettle", "area": "kitchen"},
        {"entity_id": "switch.fan_bedroom", "name": "Bedroom Fan", "area": "bedroom"},
    ],
    "climate": [
        {"entity_id": "climate.thermostat", "name": "Thermostat", "area": "living_room"},
    ],
    "covers": [
        {"entity_id": "cover.living_room_shutters", "name": "Living Room Shutters", "area": "living_room"},
        {"entity_id": "cover.bedroom_shutters", "name": "Bedroom Shutters", "area": "bedroom"},
    ],
    "locks": [
        {"entity_id": "lock.front_door", "name": "Front Door Lock", "area": "entrance"},
    ],
    "sensors": [
        {"entity_id": "sensor.living_room_temperature", "name": "Living Room Temperature", "area": "living_room"},
        {"entity_id": "sensor.living_room_humidity", "name": "Living Room Humidity", "area": "living_room"},
        {"entity_id": "binary_sensor.front_door_contact", "name": "Front Door Sensor", "area": "entrance"},
        {"entity_id": "binary_sensor.motion_living_room", "name": "Living Room Motion", "area": "living_room"},
    ],
    "scenes": [
        {"entity_id": "scene.dinner", "name": "Dinner", "description": "Dining lights on, kitchen bright, living room dimmed"},
        {"entity_id": "scene.movie_night", "name": "Movie Night", "description": "Living room dimmed, TV on, shutters closed"},
        {"entity_id": "scene.goodnight", "name": "Goodnight", "description": "All lights off, front door locked, bedroom light dim"},
        {"entity_id": "scene.morning", "name": "Morning", "description": "Kitchen and living room lights on, shutters open"},
        {"entity_id": "scene.leaving", "name": "Leaving", "description": "All lights off, all locks locked, shutters closed"},
        {"entity_id": "scene.welcome_home", "name": "Welcome Home", "description": "Hallway and living room lights on"},
    ],
    "media_players": [
        {"entity_id": "media_player.living_room_speaker", "name": "Living Room Speaker", "area": "living_room"},
        {"entity_id": "media_player.bedroom_speaker", "name": "Bedroom Speaker", "area": "bedroom"},
    ],
}


def build_entity_summary(entities: Optional[dict] = None) -> str:
    """
    Render a compact, human-readable summary of available HA entities.
    This goes into Claude's system prompt so it knows what exists.
    """
    if entities is None:
        entities = SAMPLE_ENTITIES

    lines = []

    if entities.get("lights"):
        lines.append("## Lights")
        for e in entities["lights"]:
            lines.append(f"  - {e['entity_id']} ({e['name']}, area: {e.get('area', 'unknown')})")

    if entities.get("switches"):
        lines.append("## Switches / Plugs")
        for e in entities["switches"]:
            lines.append(f"  - {e['entity_id']} ({e['name']}, area: {e.get('area', 'unknown')})")

    if entities.get("covers"):
        lines.append("## Covers (Shutters / Blinds)")
        for e in entities["covers"]:
            lines.append(f"  - {e['entity_id']} ({e['name']}, area: {e.get('area', 'unknown')})")

    if entities.get("climate"):
        lines.append("## Climate")
        for e in entities["climate"]:
            desc = e.get("description", "")
            line = f"  - {e['entity_id']} ({e['name']})"
            if desc:
                line += f" — {desc}"
            lines.append(line)

    if entities.get("locks"):
        lines.append("## Locks")
        for e in entities["locks"]:
            lines.append(f"  - {e['entity_id']} ({e['name']})")

    if entities.get("media_players"):
        lines.append("## Media Players")
        for e in entities["media_players"]:
            desc = e.get("description", "")
            line = f"  - {e['entity_id']} ({e['name']}, area: {e.get('area', 'unknown')})"
            if desc:
                line += f" — {desc}"
            lines.append(line)

    if entities.get("sensors"):
        lines.append("## Sensors (read-only)")
        for e in entities["sensors"]:
            lines.append(f"  - {e['entity_id']} ({e['name']})")

    if entities.get("scenes"):
        lines.append("## Scenes")
        for s in entities["scenes"]:
            desc = s.get("description", "")
            lines.append(f"  - {s['entity_id']} ({s['name']}) — {desc}")

    return "\n".join(lines)


def build_system_prompt(entities: Optional[dict] = None) -> str:
    """
    Full system prompt for the intent parser.
    Claude uses this to understand what devices exist and how to respond.
    """
    entity_summary = build_entity_summary(entities)

    return f"""You are Claudette's Home Control parser. Your job is to convert a user's natural language home command into a structured JSON action that Home Assistant can execute.

## Rules
1. Respond ONLY with a valid JSON object — no explanation, no markdown, no extra text.
2. If the user's request maps to a scene, prefer the scene over individual device calls.
3. If you cannot determine a valid action (ambiguous, no matching entity), return a "clarify" action.
4. For multi-step requests (e.g. "set up for dinner and lock the door"), return a list of actions.
5. Infer intent from natural language — users do NOT speak in commands.
6. Accept input in any language — English, Maltese (Malti), Italian, or Arabic. Always respond in JSON regardless of input language.

## Language examples
- Maltese: "Agħlaq id-dawl" = turn off the lights
- Maltese: "Iftaħ il-griġ" = open the shutters
- Maltese: "Sejjer norqod" = I'm going to bed (→ scene.goodnight)
- Italian: "Spegni le luci" = turn off the lights
- Italian: "Prepara la cena" = set up for dinner (→ scene.dinner)
- Arabic: "أطفئ الأضواء" = turn off the lights

## Response format
Single action:
{{
  "action": "call_service",
  "domain": "light|switch|scene|cover|lock|climate|media_player",
  "service": "turn_on|turn_off|toggle|open|close|lock|unlock|set_temperature|play_media|activate|media_play|media_pause|media_stop|media_next_track|media_previous_track|volume_set|volume_up|volume_down",
  "entity_id": "entity.id_here",
  "params": {{}}
}}

For lights with brightness/color:
{{
  "action": "call_service",
  "domain": "light",
  "service": "turn_on",
  "entity_id": "light.living_room",
  "params": {{
    "brightness_pct": 40,
    "color_temp": "warm"
  }}
}}

Multiple actions (list):
[
  {{ ...action1... }},
  {{ ...action2... }}
]

When Claudette needs to ask the user:
{{
  "action": "clarify",
  "question": "Which room did you mean?"
}}

When the request is a status query (not a command):
{{
  "action": "query",
  "entity_id": "sensor.living_room_temperature",
  "question": "What is the current temperature?"
}}

## Audio command guidance
When the user is talking about music / speakers / audio transport:
- Use `media_player` domain actions.
- Prefer the most specific matching speaker/zone entity.
- "everywhere", "whole house", "all speakers" should map to the whole-home/group player when one exists.
- "play" / "resume" → `media_play`
- "pause" / "hold on" → `media_pause`
- "stop the music" → `media_stop`
- "skip" / "next song" → `media_next_track`
- "go back" / "previous song" → `media_previous_track`
- "louder" / "turn it up" → `volume_up`
- "quieter" / "turn it down" → `volume_down`
- Exact levels like "set volume to 40%" should use `volume_set` with `params.volume_level` as 0.0–1.0
- Requests like "play jazz on the Sonos" or "play music everywhere" may use `play_media` when a concrete target/source is known.

## Available devices and scenes
{entity_summary}

## Examples
- "turn off the lights" → turn_off all lights
- "dim the living room to 40%" → light.living_room turn_on brightness_pct:40
- "it's getting dark, sort the living room" → scene.movie_night or dim lights to ~30%
- "I'm going to bed" → scene.goodnight
- "I'm heading out" → scene.leaving
- "lock the front door" → lock.front_door lock
- "close the shutters" → all covers close
- "set the dinner scene" → scene.dinner activate
- "what's the temperature?" → query sensor.living_room_temperature
- "play jazz on the Sonos" → media_player.xaghra_sitting_room play_media
- "play music everywhere" → media_player whole-house/group play_media or media_play
- "pause the music downstairs" → media_player downstairs media_pause
- "skip this track" → media_player current/active zone media_next_track
- "go back one song in the kitchen" → media_player kitchen media_previous_track
- "turn it up in the bedroom" → media_player bedroom volume_up
- "set the living room speaker to 40 percent" → media_player.living_room_speaker volume_set with volume_level 0.4
- "turn on the TV" → media_player.living_room_tv turn_on
- "set AC to 22 degrees" → climate.9424b87a6361 set_temperature 22
- "turn on quiet mode" → switch.9424b87a6361_quiet_mode turn_on
"""
