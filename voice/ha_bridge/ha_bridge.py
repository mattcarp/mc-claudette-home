#!/usr/bin/env python3
"""
Claudette Home — Home Assistant Bridge
Connects Claudette to the HA REST API and WebSocket event bus.

Written speculatively against HA API docs (issue #6).
Will be wired up once HA is installed on Workshop or a Pi.

Capabilities:
  - Execute service calls (light.turn_on, lock.lock, scene.activate, etc.)
  - Query entity states (sensor readings, lock status, etc.)
  - Subscribe to HA events via WebSocket (motion, door contact, etc.)
  - Fetch entity list for dynamic context injection into intent parser

Usage (standalone):
  python3 ha_bridge.py --action get_entities
  python3 ha_bridge.py --action call_service --payload '{"domain":"light","service":"turn_on","entity_id":"light.living_room"}'
  python3 ha_bridge.py --action get_state --entity light.living_room
  python3 ha_bridge.py --action subscribe_events  # starts WebSocket listener

As a module:
  from ha_bridge import HABridge
  bridge = HABridge()
  bridge.call_service("light", "turn_on", "light.living_room", {"brightness_pct": 50})
  state = bridge.get_state("sensor.living_room_temperature")
  entities = bridge.get_entities()

Environment:
  HA_URL      — Home Assistant base URL (default: http://localhost:8123)
  HA_TOKEN    — Long-lived access token (required)
  HA_TIMEOUT  — Request timeout in seconds (default: 10)

How to get a long-lived HA token:
  1. Log in to HA → Profile → Security → Long-Lived Access Tokens → Create Token
  2. Save it: echo 'export HA_TOKEN=eyJ...' >> /etc/environment
  3. Or store in Infisical as HA_TOKEN and inject via systemd

HA REST API docs: https://developers.home-assistant.io/docs/api/rest/
HA WebSocket API docs: https://developers.home-assistant.io/docs/api/websocket/
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Union

try:
    import requests
except ImportError:
    raise ImportError("requests not installed — run: pip install requests")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
DEFAULT_HA_TOKEN = os.environ.get("HA_TOKEN", "")
DEFAULT_TIMEOUT = int(os.environ.get("HA_TIMEOUT", "10"))


class HAError(Exception):
    """Raised when HA API returns an error or is unreachable."""
    pass


class HABridge:
    """
    Synchronous HA REST API bridge.
    Used by the intent parser to execute actions and query state.
    """

    def __init__(
        self,
        url: str = DEFAULT_HA_URL,
        token: str = DEFAULT_HA_TOKEN,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        if not token:
            raise EnvironmentError(
                "HA_TOKEN not set. Generate one at HA Profile → Long-Lived Access Tokens."
            )
        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(self.headers)

    # -----------------------------------------------------------------------
    # Health / discovery
    # -----------------------------------------------------------------------

    def ping(self) -> bool:
        """Returns True if HA is reachable and the token is valid."""
        try:
            r = self._session.get(f"{self.url}/api/", timeout=self.timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def get_config(self) -> dict:
        """Return HA config (version, location, etc.)."""
        return self._get("/api/config")

    # -----------------------------------------------------------------------
    # State queries
    # -----------------------------------------------------------------------

    def get_state(self, entity_id: str) -> dict:
        """
        Get the current state of an entity.

        Returns:
            {
              "entity_id": "light.living_room",
              "state": "on",
              "attributes": {"brightness": 200, ...},
              "last_changed": "2026-03-04T09:00:00+00:00"
            }
        """
        return self._get(f"/api/states/{entity_id}")

    def get_all_states(self) -> List[dict]:
        """Get states for all entities."""
        return self._get("/api/states")

    def get_entities(self, domains: Optional[List[str]] = None) -> dict:
        """
        Fetch all entity states and group by domain.
        Used by the intent parser context builder to get a live entity list.

        Args:
            domains: Optional filter (e.g. ["light", "switch", "scene"]).
                     If None, returns all domains Claudette cares about.

        Returns:
            Entity dict compatible with ha_context.build_system_prompt()
        """
        if domains is None:
            domains = ["light", "switch", "cover", "lock", "climate",
                       "sensor", "binary_sensor", "scene", "media_player",
                       "input_boolean", "automation"]

        all_states = self.get_all_states()
        grouped: dict = {d: [] for d in domains}

        for state in all_states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            if domain not in grouped:
                continue

            attrs = state.get("attributes", {})
            entity = {
                "entity_id": entity_id,
                "name": attrs.get("friendly_name", entity_id),
                "state": state.get("state"),
                "area": attrs.get("area", None),
            }

            # Add domain-specific useful attributes
            if domain == "light":
                entity["brightness_pct"] = (
                    round(attrs["brightness"] / 255 * 100)
                    if "brightness" in attrs else None
                )
            elif domain == "scene":
                entity["description"] = attrs.get("description", "")
            elif domain in ("sensor", "binary_sensor"):
                entity["unit"] = attrs.get("unit_of_measurement", "")

            grouped[domain].append(entity)

        # Normalize to the format ha_context.py expects
        result = {}
        for domain, entities in grouped.items():
            if entities:
                # Merge binary_sensor into sensors for simplicity
                if domain == "binary_sensor":
                    result.setdefault("sensors", []).extend(entities)
                elif domain == "sensor":
                    result.setdefault("sensors", []).extend(entities)
                else:
                    result[domain + "s" if not domain.endswith("s") else domain] = entities

        return result

    # -----------------------------------------------------------------------
    # Service calls — the main action path
    # -----------------------------------------------------------------------

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: Optional[str] = None,
        params: Optional[dict] = None,
    ) -> List[dict]:
        """
        Call a Home Assistant service.

        Args:
            domain: Service domain (light, switch, lock, scene, climate, etc.)
            service: Service name (turn_on, turn_off, lock, activate, etc.)
            entity_id: Target entity (omit for scene.activate — HA uses entity_id in params)
            params: Additional service data (brightness_pct, temperature, etc.)

        Returns:
            List of affected entity states (HA API response)

        Raises:
            HAError on failure

        Examples:
            bridge.call_service("light", "turn_on", "light.living_room", {"brightness_pct": 50})
            bridge.call_service("scene", "turn_on", "scene.goodnight")
            bridge.call_service("lock", "lock", "lock.front_door")
            bridge.call_service("climate", "set_temperature", "climate.thermostat", {"temperature": 22})
        """
        payload: dict = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if params:
            payload.update(params)

        logger.info(f"HA call_service: {domain}.{service} {payload}")
        return self._post(f"/api/services/{domain}/{service}", payload)

    def execute_action(self, action: Union[dict, list]) -> list:
        """
        Execute a parsed intent action (or list of actions) from the intent parser.

        This is the main integration point between intent_parser.py and ha_bridge.py.

        Args:
            action: dict or list of dicts as returned by intent_parser.parse_intent()

        Returns:
            List of result dicts (one per action)
        """
        if isinstance(action, list):
            results = []
            for a in action:
                results.append(self._execute_single(a))
            return results
        else:
            return [self._execute_single(action)]

    def _execute_single(self, action: dict) -> dict:
        """Execute one parsed action dict."""
        action_type = action.get("action")

        if action_type == "call_service":
            domain = action.get("domain")
            service = action.get("service")
            entity_id = action.get("entity_id")
            params = action.get("params", {})

            if not domain or not service:
                return {"error": "Missing domain or service in action", "action": action}

            try:
                result = self.call_service(domain, service, entity_id, params)
                return {"ok": True, "domain": domain, "service": service,
                        "entity_id": entity_id, "result": result}
            except HAError as e:
                return {"ok": False, "error": str(e), "action": action}

        elif action_type == "query":
            entity_id = action.get("entity_id")
            if not entity_id:
                return {"error": "Missing entity_id in query action"}
            try:
                state = self.get_state(entity_id)
                return {"ok": True, "action": "query", "entity_id": entity_id,
                        "state": state.get("state"),
                        "attributes": state.get("attributes", {})}
            except HAError as e:
                return {"ok": False, "error": str(e)}

        elif action_type == "clarify":
            # Return the clarification question — pipeline will TTS it
            return {"ok": True, "action": "clarify",
                    "question": action.get("question", "Could you clarify?")}

        else:
            return {"ok": False, "error": f"Unknown action type: {action_type}", "action": action}

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _get(self, path: str) -> Union[dict, list]:
        try:
            r = self._session.get(f"{self.url}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            raise HAError(f"HA GET {path} failed: {e.response.status_code} {e.response.text}") from e
        except requests.RequestException as e:
            raise HAError(f"HA GET {path} connection error: {e}") from e

    def _post(self, path: str, payload: dict) -> list:
        try:
            r = self._session.post(
                f"{self.url}{path}",
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            raise HAError(f"HA POST {path} failed: {e.response.status_code} {e.response.text}") from e
        except requests.RequestException as e:
            raise HAError(f"HA POST {path} connection error: {e}") from e


# ---------------------------------------------------------------------------
# WebSocket event subscriber (async)
# ---------------------------------------------------------------------------

class HAEventSubscriber:
    """
    Subscribe to Home Assistant WebSocket events.
    Used for proactive alerts (motion, door opened, etc.) — issue #8.

    Usage:
        async def on_motion(event):
            print("Motion detected!", event)

        sub = HAEventSubscriber(token=HA_TOKEN)
        await sub.subscribe("state_changed", on_motion)
    """

    def __init__(self, url: str = DEFAULT_HA_URL, token: str = DEFAULT_HA_TOKEN):
        self.ws_url = url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        self.token = token
        self._msg_id = 1

    async def subscribe(
        self,
        event_type: str,
        callback: Callable[[dict], None],
        entity_filter: Optional[List[str]] = None,
    ):
        """
        Subscribe to HA WebSocket events.

        Args:
            event_type: "state_changed", "call_service", etc.
            callback: async function called on each matching event
            entity_filter: if set, only call callback for these entity_ids
        """
        try:
            import websockets
        except ImportError:
            raise ImportError("websockets not installed — run: pip install websockets")

        logger.info(f"Connecting to HA WebSocket: {self.ws_url}")

        async with websockets.connect(self.ws_url) as ws:
            # Step 1: Auth handshake
            auth_required = json.loads(await ws.recv())
            assert auth_required["type"] == "auth_required", f"Unexpected: {auth_required}"

            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth_ok = json.loads(await ws.recv())
            if auth_ok["type"] != "auth_ok":
                raise HAError(f"HA WebSocket auth failed: {auth_ok}")

            logger.info("HA WebSocket authenticated")

            # Step 2: Subscribe to event type
            sub_msg = {
                "id": self._msg_id,
                "type": "subscribe_events",
                "event_type": event_type,
            }
            self._msg_id += 1
            await ws.send(json.dumps(sub_msg))
            sub_ack = json.loads(await ws.recv())
            if not sub_ack.get("success"):
                raise HAError(f"Failed to subscribe to {event_type}: {sub_ack}")

            logger.info(f"Subscribed to HA events: {event_type}")

            # Step 3: Receive events
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue

                event = msg.get("event", {})

                # Filter by entity_id if requested
                if entity_filter and event_type == "state_changed":
                    changed_id = event.get("data", {}).get("entity_id")
                    if changed_id not in entity_filter:
                        continue

                await callback(event)


# ---------------------------------------------------------------------------
# Stub mode — for dev/testing without a live HA instance
# ---------------------------------------------------------------------------

class HABridgeStub:
    """
    Stub bridge for development — no live HA required.
    Logs actions to stdout. Returns fake state data.
    Used by pipeline.py in STUB_MODE.
    """

    def __init__(self, *args, **kwargs):
        logger.info("HABridge running in STUB MODE — no live HA connection")

    def ping(self) -> bool:
        return True

    def get_entities(self, domains=None) -> dict:
        from sys import path as spath
        import os
        spath.insert(0, os.path.join(os.path.dirname(__file__), "../intent_parser"))
        from ha_context import SAMPLE_ENTITIES
        return SAMPLE_ENTITIES

    def get_state(self, entity_id: str) -> dict:
        return {"entity_id": entity_id, "state": "unknown", "attributes": {}}

    def call_service(self, domain, service, entity_id=None, params=None) -> list:
        logger.info(f"[STUB] call_service: {domain}.{service} {entity_id} params={params}")
        print(f"[STUB] → {domain}.{service}({entity_id}) params={params}", flush=True)
        return []

    def execute_action(self, action: Union[dict, list]) -> list:
        if isinstance(action, list):
            return [self._execute_single(a) for a in action]
        return [self._execute_single(action)]

    def _execute_single(self, action: dict) -> dict:
        action_type = action.get("action")
        if action_type == "call_service":
            print(f"[STUB] HA action: {action.get('domain')}.{action.get('service')}({action.get('entity_id')}) {action.get('params', {})}", flush=True)
            return {"ok": True, "stub": True, "action": action}
        elif action_type == "clarify":
            return {"ok": True, "action": "clarify", "question": action.get("question")}
        elif action_type == "query":
            return {"ok": True, "action": "query", "state": "stub", "entity_id": action.get("entity_id")}
        return {"ok": False, "error": "unknown action type", "action": action}


def get_bridge(stub: bool = False) -> Union[HABridge, HABridgeStub]:
    """Factory — returns a live or stub bridge based on env/flag."""
    if stub or not os.environ.get("HA_TOKEN"):
        return HABridgeStub()
    return HABridge()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Claudette Home — HA Bridge CLI")
    parser.add_argument("--action", choices=["ping", "get_entities", "get_state", "call_service", "subscribe_events"],
                        required=True)
    parser.add_argument("--entity", help="Entity ID (for get_state, call_service)")
    parser.add_argument("--payload", help="JSON payload for call_service or action execution")
    parser.add_argument("--event-type", default="state_changed", help="Event type for subscribe_events")
    parser.add_argument("--stub", action="store_true", help="Use stub mode (no HA required)")
    args = parser.parse_args()

    bridge = get_bridge(stub=args.stub)

    if args.action == "ping":
        ok = bridge.ping()
        print(f"HA reachable: {ok}")
        sys.exit(0 if ok else 1)

    elif args.action == "get_entities":
        entities = bridge.get_entities()
        print(json.dumps(entities, indent=2))

    elif args.action == "get_state":
        if not args.entity:
            print("Error: --entity required", file=sys.stderr)
            sys.exit(1)
        state = bridge.get_state(args.entity)
        print(json.dumps(state, indent=2))

    elif args.action == "call_service":
        if not args.payload:
            print("Error: --payload required (JSON with domain, service, entity_id)", file=sys.stderr)
            sys.exit(1)
        action = json.loads(args.payload)
        result = bridge.execute_action(action)
        print(json.dumps(result, indent=2))

    elif args.action == "subscribe_events":
        async def on_event(event):
            print(json.dumps(event), flush=True)

        asyncio.run(
            HAEventSubscriber().subscribe(args.event_type, on_event)
        )


if __name__ == "__main__":
    main()
