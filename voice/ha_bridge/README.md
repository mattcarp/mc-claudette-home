# HA Bridge

Home Assistant REST + WebSocket bridge for Claudette Home.

Written speculatively against HA API docs. Wired up once HA is installed (issue #12).

## Setup

```bash
# 1. Install HA (Docker on Workshop)
docker run -d --name homeassistant \
  -v /home/sysop/homeassistant:/config \
  -p 8123:8123 \
  --restart unless-stopped \
  ghcr.io/home-assistant/home-assistant:stable

# 2. Complete HA onboarding at http://localhost:8123

# 3. Generate a long-lived access token
#    HA → Profile → Security → Long-Lived Access Tokens → Create Token

# 4. Set env vars
echo 'export HA_URL=http://localhost:8123' >> /etc/environment
echo 'export HA_TOKEN=eyJ...' >> /etc/environment  # or use Infisical

# 5. Test
pip install -r requirements.txt
python3 ha_bridge.py --action ping
python3 ha_bridge.py --action get_entities
```

## Usage

```bash
# Ping HA
python3 ha_bridge.py --action ping

# Get all entities (for intent parser context)
python3 ha_bridge.py --action get_entities

# Query a state
python3 ha_bridge.py --action get_state --entity sensor.living_room_temperature

# Execute an action
python3 ha_bridge.py --action call_service \
  --payload '{"action":"call_service","domain":"light","service":"turn_on","entity_id":"light.living_room","params":{"brightness_pct":50}}'

# Stub mode (dev — no HA needed)
python3 ha_bridge.py --action get_entities --stub

# Subscribe to events (prints JSON lines)
python3 ha_bridge.py --action subscribe_events --event-type state_changed
```

## As a module

```python
from ha_bridge import HABridge, get_bridge

# Live bridge
bridge = HABridge()  # reads HA_URL + HA_TOKEN from env

# Or stub for dev
bridge = get_bridge(stub=True)

# Check connection
bridge.ping()  # True/False

# Get entities (live list, works with ha_context.build_system_prompt)
entities = bridge.get_entities()

# Execute an intent parser action directly
from intent_parser import parse_intent
action = parse_intent("turn off the living room lights")
results = bridge.execute_action(action)

# Query state
state = bridge.get_state("sensor.living_room_temperature")
# {"entity_id": "sensor.living_room_temperature", "state": "22.5", "attributes": {"unit_of_measurement": "°C"}}
```

## Architecture

```
wake_word_bridge.py → (stdout JSON events)
     ↓
pipeline.py (orchestrator)
     ↓
transcribe_api.py (STT via Whisper)
     ↓
intent_parser.py (NL → HA action via Claude)
     ↓
ha_bridge.py (execute action via HA REST API)
     ↓
Home Assistant
     ↓
Zigbee/WiFi/Z-Wave devices (lights, locks, shutters...)
```

## WebSocket events (issue #8 — proactive alerts)

```python
import asyncio
from ha_bridge import HAEventSubscriber

async def on_door_opened(event):
    entity_id = event["data"]["entity_id"]
    new_state = event["data"]["new_state"]["state"]
    if entity_id == "binary_sensor.front_door_contact" and new_state == "on":
        print("Front door opened!")
        # → TTS alert, log to Mission Control, etc.

asyncio.run(
    HAEventSubscriber().subscribe(
        "state_changed",
        on_door_opened,
        entity_filter=["binary_sensor.front_door_contact"]
    )
)
```

## Blocked on

- HA installed (issue #12) — needs Docker on Workshop or Pi
- HA_TOKEN generated and stored in /etc/environment
- Zigbee dongle for Zigbee devices (optional — WiFi devices work without it)
