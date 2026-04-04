#!/usr/bin/env python3
"""
Claudette Home — Live Entity Sync from Home Assistant
Reads real entity/device data from HA (REST API or local .storage files)
and prints or writes a fresh REAL_ENTITIES dict for ha_context.py.

Two modes:
  1. API mode  — requires HA_TOKEN. Calls /api/states + /api/areas.
                 Most accurate: includes current state, area assignments.
  2. Storage mode (default) — reads HA .storage JSON directly (no token).
                  Fast, always works locally, slightly less rich.

Usage:
  python3 voice/intent_parser/refresh_entities.py            # storage mode, print
  python3 voice/intent_parser/refresh_entities.py --api      # API mode, print
  python3 voice/intent_parser/refresh_entities.py --write    # update ha_context.py
  python3 voice/intent_parser/refresh_entities.py --api --write

Environment:
  HA_URL      — HA base URL (default: http://localhost:8123)
  HA_TOKEN    — Long-lived access token (required for --api mode)
  HA_STORAGE  — Path to HA .storage dir (default: ~/homeassistant/.storage)
"""

import argparse
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------
HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_STORAGE = Path(os.environ.get("HA_STORAGE", Path.home() / "homeassistant" / ".storage"))

# Domains we care about for voice control
DOMAIN_MAP = {
    "light": "lights",
    "switch": "switches",
    "media_player": "media_players",
    "climate": "climate",
    "cover": "covers",
    "lock": "locks",
    "scene": "scenes",
    "sensor": "sensors",
    "binary_sensor": "sensors",
    "weather": "sensors",
}

# Switches to exclude from voice control (HA internal / noise)
EXCLUDED_ENTITY_PREFIXES = [
    "switch.hacs_",
    "switch.eufy_security_pre_release",
    "update.",
    "sensor.backup_",
    "sensor.sonos_favorites",
    "binary_sensor.sun_",
    "sensor.sun_",
    "event.",
    "person.",
    "tts.",
]


def should_exclude(entity_id: str) -> bool:
    return any(entity_id.startswith(p) for p in EXCLUDED_ENTITY_PREFIXES)


# ---------------------------------------------------------------------------
# Storage mode: parse HA .storage files directly
# ---------------------------------------------------------------------------

def load_storage_json(name: str) -> dict:
    path = HA_STORAGE / name
    if not path.exists():
        log.warning(f"Storage file not found: {path}")
        return {}
    with open(path) as f:
        return json.load(f)


def fetch_storage_entities() -> List[dict]:
    """Read entities from core.entity_registry + core.device_registry."""
    entity_data = load_storage_json("core.entity_registry")
    device_data = load_storage_json("core.device_registry")

    # Build device id → name map
    device_names: Dict[str, str] = {}
    for dev in device_data.get("data", {}).get("devices", []):
        dev_id = dev.get("id", "")
        name = (
            dev.get("name_by_user")
            or dev.get("name")
            or dev.get("manufacturer", "?")
        )
        device_names[dev_id] = name

    entities = []
    for e in entity_data.get("data", {}).get("entities", []):
        entity_id = e.get("entity_id", "")
        if not entity_id or should_exclude(entity_id):
            continue
        domain = entity_id.split(".")[0]
        if domain not in DOMAIN_MAP:
            continue

        device_id = e.get("device_id", "")
        device_name = device_names.get(device_id, "")

        entities.append({
            "entity_id": entity_id,
            "name": e.get("name") or e.get("original_name") or device_name or entity_id.split(".")[-1].replace("_", " ").title(),
            "area_id": e.get("area_id") or "",
            "platform": e.get("platform", ""),
            "domain": domain,
            "device_name": device_name,
        })

    return entities


# ---------------------------------------------------------------------------
# API mode: call HA REST API
# ---------------------------------------------------------------------------

def ha_get(path: str) -> Any:
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HA_TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def fetch_api_entities() -> List[dict]:
    """Fetch entity states + area registry from live HA API."""
    states = ha_get("/api/states")

    # Fetch area registry
    try:
        area_data = ha_get("/api/config/area_registry/list")
        area_map = {a["area_id"]: a["name"] for a in area_data}
    except Exception:
        area_map = {}

    # Fetch entity registry for area assignments
    try:
        entity_registry = ha_get("/api/config/entity_registry/list")
        entity_area_map = {e["entity_id"]: e.get("area_id", "") for e in entity_registry}
    except Exception:
        entity_area_map = {}

    entities = []
    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id or should_exclude(entity_id):
            continue
        domain = entity_id.split(".")[0]
        if domain not in DOMAIN_MAP:
            continue

        attrs = state.get("attributes", {})
        area_id = entity_area_map.get(entity_id, "")
        area_name = area_map.get(area_id, "").lower().replace(" ", "_") if area_id else ""

        entities.append({
            "entity_id": entity_id,
            "name": attrs.get("friendly_name") or entity_id.split(".")[-1].replace("_", " ").title(),
            "area": area_name,
            "area_id": area_id,
            "domain": domain,
            "state": state.get("state", ""),
        })

    return entities


# ---------------------------------------------------------------------------
# Build structured REAL_ENTITIES dict
# ---------------------------------------------------------------------------

AREA_ALIASES = {
    "sitting_room": "sitting_room",
    "sitting": "sitting_room",
    "lounge": "sitting_room",
    "living_room": "living_room",
    "living": "living_room",
    "kitchen": "kitchen",
    "bedroom": "bedroom",
    "master_bedroom": "bedroom",
    "bathroom": "bathroom",
    "entrance": "entrance",
    "hallway": "hallway",
    "garden": "garden",
    "courtyard": "garden",
    "outdoor": "outdoor",
    "": "unknown",
}


def normalise_area(area: str) -> str:
    key = area.lower().replace(" ", "_").strip()
    return AREA_ALIASES.get(key, key or "unknown")


# Friendly name overrides for known entity_ids
FRIENDLY_NAMES = {
    "media_player.xaghra_sitting_room": "Sonos Arc (Sitting Room)",
    "media_player.xaghra_sittiing_room_wiim": "WiiM Ultra (Sitting Room)",
    "media_player.living_room_tv": "Living Room TV",
    "media_player.bedroom_tv": "Bedroom TV",
    "climate.9424b87a6361": "Air Conditioner (Sitting Room)",
    "switch.9424b87a6361_panel_light": "AC Panel Light",
    "switch.9424b87a6361_quiet_mode": "AC Quiet Mode",
    "switch.9424b87a6361_fresh_air": "AC Fresh Air",
    "switch.9424b87a6361_xtra_fan": "AC Extra Fan",
    "switch.9424b87a6361_health_mode": "AC Health Mode",
    "switch.xaghra_sitting_room_loudness": "Sonos Loudness",
    "switch.xaghra_sitting_room_night_sound": "Sonos Night Mode",
    "switch.xaghra_sitting_room_speech_enhancement": "Sonos Speech Enhancement",
    "switch.xaghra_sitting_room_crossfade": "Sonos Crossfade",
    "switch.xaghra_sitting_room_status_light": "Sonos Status Light",
    "todo.shopping_list": "Shopping List",
    "weather.forecast_home": "Home Weather",
    "sensor.sun_next_rising": "Sunrise Time",
    "sensor.sun_next_setting": "Sunset Time",
    "sensor.sun_solar_elevation": "Solar Elevation",
}

AREA_OVERRIDES = {
    "media_player.xaghra_sitting_room": "sitting_room",
    "media_player.xaghra_sittiing_room_wiim": "sitting_room",
    "media_player.living_room_tv": "living_room",
    "media_player.bedroom_tv": "bedroom",
    "climate.9424b87a6361": "sitting_room",
    "switch.9424b87a6361_panel_light": "sitting_room",
    "switch.9424b87a6361_quiet_mode": "sitting_room",
    "switch.9424b87a6361_fresh_air": "sitting_room",
    "switch.9424b87a6361_xtra_fan": "sitting_room",
    "switch.9424b87a6361_health_mode": "sitting_room",
    "switch.xaghra_sitting_room_loudness": "sitting_room",
    "switch.xaghra_sitting_room_night_sound": "sitting_room",
    "switch.xaghra_sitting_room_speech_enhancement": "sitting_room",
    "switch.xaghra_sitting_room_crossfade": "sitting_room",
    "switch.xaghra_sitting_room_status_light": "sitting_room",
    "weather.forecast_home": "outdoor",
    "sensor.sun_next_rising": "outdoor",
    "sensor.sun_next_setting": "outdoor",
    "sensor.sun_solar_elevation": "outdoor",
}


def build_real_entities(raw: List[dict]) -> Dict[str, List[dict]]:
    """Convert flat entity list into categorised REAL_ENTITIES dict."""
    result: Dict[str, List[dict]] = {
        "lights": [],
        "switches": [],
        "media_players": [],
        "climate": [],
        "covers": [],
        "locks": [],
        "scenes": [],
        "sensors": [],
    }

    for e in raw:
        domain = e["domain"]
        category = DOMAIN_MAP.get(domain, "sensors")
        entity_id = e["entity_id"]

        area = AREA_OVERRIDES.get(entity_id) or normalise_area(e.get("area", "") or e.get("area_id", ""))
        name = FRIENDLY_NAMES.get(entity_id) or e.get("name") or entity_id

        entry: dict = {
            "entity_id": entity_id,
            "name": name,
            "area": area,
        }

        # Add description for known devices
        descriptions = {
            "media_player.xaghra_sitting_room": "Sonos Arc soundbar",
            "media_player.xaghra_sittiing_room_wiim": "WiiM Ultra streamer",
            "media_player.living_room_tv": "Sony BRAVIA 4K main TV",
            "media_player.bedroom_tv": "Sony BRAVIA bedroom TV",
            "climate.9424b87a6361": "Gree split AC unit",
            "weather.forecast_home": "MET weather forecast",
        }
        if entity_id in descriptions:
            entry["description"] = descriptions[entity_id]

        # Deduplicate: don't add if entity_id already present
        existing_ids = [x["entity_id"] for x in result[category]]
        if entity_id not in existing_ids:
            result[category].append(entry)

    # Sort each category by area, then entity_id
    for cat in result:
        result[cat].sort(key=lambda x: (x.get("area", ""), x["entity_id"]))

    return result


# ---------------------------------------------------------------------------
# Format as Python source for embedding in ha_context.py
# ---------------------------------------------------------------------------

def format_entity_list(entities: List[dict]) -> str:
    lines = []
    for e in entities:
        parts = [f'"entity_id": "{e["entity_id"]}"']
        parts.append(f'"name": "{e["name"]}"')
        parts.append(f'"area": "{e.get("area", "unknown")}"')
        if "description" in e:
            parts.append(f'"description": "{e["description"]}"')
        line = "        {" + ", ".join(parts) + "},"
        lines.append(line)
    return "\n".join(lines)


def format_as_python(entities_dict: Dict[str, List[dict]], source: str) -> str:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    sections = []
    for cat, items in entities_dict.items():
        if not items:
            continue
        inner = format_entity_list(items)
        sections.append(f'    "{cat}": [\n{inner}\n    ],')

    body = "\n".join(sections)
    return f"""# ---------------------------------------------------------------------------
# REAL entities — auto-synced from live HA device/entity registry {ts}
# Source: {source}
# Re-run: python3 voice/intent_parser/refresh_entities.py [--api] [--write]
# ---------------------------------------------------------------------------
REAL_ENTITIES = {{
{body}
}}
"""


# ---------------------------------------------------------------------------
# Optional: patch ha_context.py in place
# ---------------------------------------------------------------------------

def update_ha_context(new_block: str, context_path: Path) -> None:
    original = context_path.read_text()

    # Find the REAL_ENTITIES block and replace it
    start_marker = "# ---------------------------------------------------------------------------\n# REAL entities"
    end_marker = "\n}"

    start = original.find(start_marker)
    if start == -1:
        print("ERROR: Could not find REAL_ENTITIES block in ha_context.py", file=sys.stderr)
        sys.exit(1)

    # Find the closing brace of REAL_ENTITIES
    end_search_from = original.find("REAL_ENTITIES = {", start)
    brace_depth = 0
    end_pos = end_search_from
    for i, ch in enumerate(original[end_search_from:], end_search_from):
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0:
                end_pos = i + 1
                break

    # Skip any trailing newline
    if end_pos < len(original) and original[end_pos] == "\n":
        end_pos += 1

    updated = original[:start] + new_block + original[end_pos:]
    context_path.write_text(updated)
    print(f"✅ Updated {context_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync REAL_ENTITIES from live HA")
    parser.add_argument("--api", action="store_true", help="Use HA REST API (requires HA_TOKEN)")
    parser.add_argument("--write", action="store_true", help="Write updated REAL_ENTITIES to ha_context.py")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of Python")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    # Fetch entities
    if args.api:
        if not HA_TOKEN:
            print("ERROR: HA_TOKEN not set. Export it or use storage mode (omit --api).", file=sys.stderr)
            sys.exit(1)
        print("📡 Fetching from HA REST API...", file=sys.stderr)
        raw = fetch_api_entities()
        source = f"HA REST API ({HA_URL})"
    else:
        print("📂 Reading from HA .storage files...", file=sys.stderr)
        raw = fetch_storage_entities()
        source = f"HA .storage ({HA_STORAGE})"

    print(f"   Found {len(raw)} entities across relevant domains.", file=sys.stderr)

    entities_dict = build_real_entities(raw)

    # Count
    totals = {k: len(v) for k, v in entities_dict.items() if v}
    print(f"   Categories: {totals}", file=sys.stderr)

    if args.json:
        print(json.dumps(entities_dict, indent=2))
        return

    python_block = format_as_python(entities_dict, source)

    if args.write:
        context_path = Path(__file__).parent / "ha_context.py"
        if not context_path.exists():
            print(f"ERROR: {context_path} not found", file=sys.stderr)
            sys.exit(1)
        update_ha_context(python_block, context_path)
    else:
        print(python_block)


if __name__ == "__main__":
    main()
