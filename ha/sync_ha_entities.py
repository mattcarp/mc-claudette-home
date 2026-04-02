#!/usr/bin/env python3
"""
Claudette Home — HA Entity Sync
Reads all entities from live Home Assistant and:
  1. Prints a categorised summary to stdout
  2. Optionally updates voice/intent_parser/ha_context.py REAL_ENTITIES block

Usage:
  python3 ha/sync_ha_entities.py              # print summary
  python3 ha/sync_ha_entities.py --update     # update ha_context.py REAL_ENTITIES

Requirements:
  HA_TOKEN env var must be set (HA long-lived access token)
  HA_URL  env var optional (default: http://localhost:8123)

How to generate HA_TOKEN:
  1. Log in to HA at http://localhost:8123
  2. Click your profile avatar → Security
  3. Scroll to "Long-Lived Access Tokens" → Create Token
  4. Copy token → sudo bash -c 'echo HA_TOKEN=<token> >> /etc/environment'
  5. source /etc/environment  (or restart shell)
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    print("ERROR: requests not installed — pip install requests")
    sys.exit(1)

HA_URL = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Entity domains we care about for voice control
INTERESTING_DOMAINS = {
    "light", "switch", "media_player", "climate", "cover",
    "lock", "fan", "scene", "sensor", "binary_sensor", "input_boolean",
}

# Domains to exclude from summary (internal/noisy)
EXCLUDE_DOMAINS = {
    "sun", "person", "weather", "event", "number", "select",
    "text", "button", "todo", "update", "tts", "sensor.backup",
}

# Prefixes to skip (HA internal)
SKIP_PREFIXES = {
    "sensor.backup_", "switch.hacs_", "switch.eufy_security_pre",
    "update.", "event.",
}


def fetch_states() -> List[Dict]:
    """Fetch all entity states from HA REST API."""
    if not HA_TOKEN:
        print("ERROR: HA_TOKEN not set. See usage in module docstring.")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{HA_URL}/api/states"
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code == 401:
        print("ERROR: HA token invalid or expired. Generate a new one.")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def categorise_entities(states: List[Dict]) -> Dict[str, List[Dict]]:
    """Sort HA entities into categories for the intent parser."""
    categories: Dict[str, List[Dict]] = {
        "lights": [],
        "switches": [],
        "media_players": [],
        "climate": [],
        "covers": [],
        "locks": [],
        "sensors": [],
        "binary_sensors": [],
        "scenes": [],
        "fans": [],
    }

    for state in states:
        eid = state.get("entity_id", "")
        domain = eid.split(".")[0]
        attrs = state.get("attributes", {})

        # Skip internal / excluded entities
        if any(eid.startswith(p) for p in SKIP_PREFIXES):
            continue
        if domain not in INTERESTING_DOMAINS:
            continue

        friendly_name = attrs.get("friendly_name", eid)
        area = attrs.get("area_id") or ""
        device_class = attrs.get("device_class", "")

        entry = {
            "entity_id": eid,
            "name": friendly_name,
            "area": area,
            "state": state.get("state", ""),
        }

        if domain == "light":
            categories["lights"].append(entry)
        elif domain == "switch":
            # Filter out Sonos/HACS/Eufy internal switches for voice
            if any(skip in eid for skip in ["crossfade", "surround", "subwoofer", "touch_controls",
                                              "music_full_volume", "status_light", "audio_delay",
                                              "pre_release"]):
                continue
            categories["switches"].append(entry)
        elif domain == "media_player":
            categories["media_players"].append(entry)
        elif domain == "climate":
            categories["climate"].append(entry)
        elif domain == "cover":
            categories["covers"].append(entry)
        elif domain == "lock":
            categories["locks"].append(entry)
        elif domain == "fan":
            categories["fans"].append(entry)
        elif domain == "scene":
            categories["scenes"].append({
                "entity_id": eid,
                "name": friendly_name,
                "description": attrs.get("description", ""),
            })
        elif domain == "sensor":
            # Only useful sensors
            if device_class in ("temperature", "humidity", "energy", "power", "battery"):
                categories["sensors"].append(entry)
            elif "temperature" in eid or "humidity" in eid:
                categories["sensors"].append(entry)
        elif domain == "binary_sensor":
            if device_class in ("door", "window", "motion", "occupancy", "presence"):
                categories["binary_sensors"].append(entry)

    return categories


def print_summary(categories: Dict[str, List[Dict]]) -> None:
    """Print a human-readable summary of discovered entities."""
    total = sum(len(v) for v in categories.values())
    print(f"\n=== HA Entity Discovery — {HA_URL} ===")
    print(f"Total voice-relevant entities: {total}\n")

    for cat, items in categories.items():
        if not items:
            continue
        print(f"## {cat.upper()} ({len(items)})")
        for item in items:
            state_str = f" [state={item.get('state', '?')}]" if item.get("state") else ""
            print(f"  {item['entity_id']} — {item['name']}{state_str}")
        print()


def main() -> None:
    update_mode = "--update" in sys.argv

    print(f"Connecting to HA at {HA_URL}...")
    states = fetch_states()
    print(f"  {len(states)} entities fetched.")

    categories = categorise_entities(states)
    print_summary(categories)

    if update_mode:
        update_ha_context(categories)
    else:
        print("\nRun with --update to write REAL_ENTITIES to ha_context.py")


def update_ha_context(categories: Dict[str, List[Dict]]) -> None:
    """Write updated REAL_ENTITIES block to ha_context.py."""
    context_path = Path(__file__).parent.parent / "voice" / "intent_parser" / "ha_context.py"
    if not context_path.exists():
        print(f"ERROR: ha_context.py not found at {context_path}")
        return

    # Build the new REAL_ENTITIES block
    lines = ["REAL_ENTITIES = {"]

    for cat, items in categories.items():
        if not items:
            lines.append(f'    "{cat}": [],')
            continue
        lines.append(f'    "{cat}": [')
        for item in items:
            name = item["name"].replace('"', '\\"')
            eid = item["entity_id"]
            area = item.get("area", "") or ""
            desc = item.get("description", "") or ""
            lines.append(f'        {{')
            lines.append(f'            "entity_id": "{eid}",')
            lines.append(f'            "name": "{name}",')
            lines.append(f'            "area": "{area}",')
            if desc:
                lines.append(f'            "description": "{desc}",')
            lines.append(f'        }},')
        lines.append("    ],")

    lines.append("}")
    new_block = "\n".join(lines)

    content = context_path.read_text()

    # Replace REAL_ENTITIES block
    import re
    pattern = r"REAL_ENTITIES\s*=\s*\{.*?\n\}"
    new_content = re.sub(pattern, new_block, content, flags=re.DOTALL)

    if new_content == content:
        print("WARNING: Could not find REAL_ENTITIES block to replace — check ha_context.py format.")
        return

    context_path.write_text(new_content)
    print(f"✅ Updated REAL_ENTITIES in {context_path}")


if __name__ == "__main__":
    main()
