#!/usr/bin/env python3
"""
Claudette Home — Proactive Alert Engine

Monitors device state events and generates alerts when:
- Door/window left open > threshold (default 30 min)
- Lights left on in empty room > threshold (default 60 min, motion-aware)
- Temperature out of comfort range
- Unusual motion at odd hours (security)
- Device not responding (failure detection)

Alerts have priority levels:
- high: security and safety (doors, unusual motion, device failure)
- low: comfort and convenience (lights, temperature)

Part of EPIC 1 (#1) and Issue #8.
"""

import json
import datetime
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS = {
    "door_open_min": 30,
    "window_open_min": 30,
    "light_empty_room_min": 60,
    "temp_low_celsius": 17.0,
    "temp_high_celsius": 30.0,
    "unusual_motion_start_hour": 1,   # 01:00
    "unusual_motion_end_hour": 5,     # 05:00
    "device_timeout_min": 10,         # No response for 10 min = failure
}

# Map room names from entity_id to area slug for motion cross-reference
# e.g. "light.kitchen" → area "kitchen", check "binary_sensor.motion_kitchen"
AREA_MOTION_SENSOR_PREFIX = "binary_sensor.motion_"


class ProactiveAlerts:
    """
    Stateful alert engine. Feed it HA state_changed events via process_event()
    and it evaluates rules, generating alerts when thresholds are met.

    Alerts accumulate in active_alerts until consumed via get_pending_alerts().
    """

    def __init__(self, thresholds: Optional[dict] = None):
        self.device_states: dict[str, dict] = {}
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.active_alerts: list[dict] = []
        # Track last-seen timestamps for device failure detection
        self._last_seen: dict[str, float] = {}
        # Track motion per area (area_slug → last_motion_timestamp)
        self._area_motion: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def process_event(self, event_json: str, eval_time: Optional[float] = None) -> None:
        """
        Process an incoming Home Assistant state_changed event (JSON string).

        The event's "timestamp" is when the state CHANGED.
        eval_time is when we EVALUATE rules (defaults to time.time()).
        This distinction matters: a door that opened 35 min ago should be
        evaluated at "now", not at the event time.
        """
        try:
            event = json.loads(event_json)
        except (json.JSONDecodeError, TypeError):
            return

        entity_id = event.get("entity_id")
        if not entity_id:
            return

        state = event.get("state")
        event_timestamp = event.get("timestamp", time.time())
        now = eval_time if eval_time is not None else time.time()

        # Record when we last heard from this device (use eval_time for recency)
        self._last_seen[entity_id] = now

        # Track motion sensor activity for room-awareness
        if entity_id.startswith(AREA_MOTION_SENSOR_PREFIX) and state == "on":
            area = entity_id[len(AREA_MOTION_SENSOR_PREFIX):]
            self._area_motion[area] = event_timestamp

        # Update state cache — only reset alert flag on actual state change
        prev = self.device_states.get(entity_id, {})
        if prev.get("state") != state:
            self.device_states[entity_id] = {
                "state": state,
                "last_changed": event_timestamp,
                "alerted": False,
            }
        else:
            # Same state, just update last_seen (for keepalive)
            if entity_id in self.device_states:
                self.device_states[entity_id].setdefault("last_changed", event_timestamp)

        self.evaluate_alerts(now)

    # ------------------------------------------------------------------
    # Alert evaluation
    # ------------------------------------------------------------------

    def evaluate_alerts(self, current_time: float) -> None:
        """Run all alert rules against current device states."""
        for entity_id, data in list(self.device_states.items()):
            state = data.get("state")
            last_changed = data.get("last_changed", current_time)
            alerted = data.get("alerted", False)

            if alerted:
                continue

            elapsed_min = (current_time - last_changed) / 60

            # Rule 1: Door left open
            if (entity_id.startswith("binary_sensor.door_")
                    or entity_id.startswith("binary_sensor.front_door")):
                if state == "on" and elapsed_min >= self.thresholds["door_open_min"]:
                    name = self._friendly_name(entity_id)
                    self._fire(
                        entity_id,
                        f"Just so you know, the {name} has been open for {int(elapsed_min)} minutes.",
                        priority="high",
                    )

            # Rule 2: Window left open
            if entity_id.startswith("binary_sensor.window_"):
                if state == "on" and elapsed_min >= self.thresholds["window_open_min"]:
                    name = self._friendly_name(entity_id)
                    self._fire(
                        entity_id,
                        f"The {name} has been open for {int(elapsed_min)} minutes.",
                        priority="low",
                    )

            # Rule 3: Lights on in empty room (motion-aware)
            if entity_id.startswith("light.") and state == "on":
                if elapsed_min >= self.thresholds["light_empty_room_min"]:
                    area = self._area_from_entity(entity_id)
                    if not self._recent_motion_in_area(area, current_time,
                                                       window_min=self.thresholds["light_empty_room_min"]):
                        name = self._friendly_name(entity_id)
                        self._fire(
                            entity_id,
                            f"The {name} light has been on for over an hour with no motion. Want me to turn it off?",
                            priority="low",
                        )

            # Rule 4: Temperature out of comfort range
            if entity_id.startswith("sensor.") and "temperature" in entity_id:
                if state and self._is_numeric(state):
                    temp = float(state)
                    if temp < self.thresholds["temp_low_celsius"]:
                        self._fire(
                            entity_id,
                            f"Temperature dropped to {temp}°C. Heating is off, want me to turn it on?",
                            priority="low",
                        )
                    elif temp > self.thresholds["temp_high_celsius"]:
                        self._fire(
                            entity_id,
                            f"Temperature is up to {temp}°C. Want me to turn on the AC?",
                            priority="low",
                        )

            # Rule 5: Unusual motion at odd hours
            if entity_id.startswith(AREA_MOTION_SENSOR_PREFIX) and state == "on":
                # Use the time the motion actually happened (last_changed), not eval time
                hour = self._hour_from_timestamp(last_changed)
                start = self.thresholds["unusual_motion_start_hour"]
                end = self.thresholds["unusual_motion_end_hour"]
                if start <= hour < end:
                    area = entity_id[len(AREA_MOTION_SENSOR_PREFIX):]
                    self._fire(
                        entity_id,
                        f"Motion detected in {area.replace('_', ' ')} at an unusual hour.",
                        priority="high",
                    )

        # Rule 6: Device failure — check for devices that went silent
        self._check_device_failures(current_time)

    def _check_device_failures(self, current_time: float) -> None:
        """Alert if a tracked device hasn't reported in > device_timeout_min."""
        timeout_sec = self.thresholds["device_timeout_min"] * 60
        for entity_id, last_seen in list(self._last_seen.items()):
            # Only track sensors and switches (not passive entities)
            if not (entity_id.startswith("sensor.") or entity_id.startswith("binary_sensor.")):
                continue
            silence = current_time - last_seen
            if silence >= timeout_sec:
                # Only alert once per silence window
                failure_key = f"_failure_{entity_id}"
                if failure_key not in self.device_states or not self.device_states[failure_key].get("alerted"):
                    name = self._friendly_name(entity_id)
                    self.device_states[failure_key] = {"alerted": True, "state": "failure"}
                    self._fire(
                        entity_id,
                        f"The {name} hasn't responded for {int(silence / 60)} minutes. It may need attention.",
                        priority="high",
                    )

    # ------------------------------------------------------------------
    # Alert dispatch
    # ------------------------------------------------------------------

    def _fire(self, entity_id: str, message: str, priority: str = "low") -> None:
        """Record an alert and mark entity as alerted."""
        self.active_alerts.append({
            "entity": entity_id,
            "message": message,
            "priority": priority,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        if entity_id in self.device_states:
            self.device_states[entity_id]["alerted"] = True

    def trigger_alert(self, entity_id: str, message: str, priority: str = "low") -> None:
        """Public interface for trigger_alert (backwards compat)."""
        self._fire(entity_id, message, priority)

    def get_pending_alerts(self) -> list[dict]:
        """Fetch and clear pending alerts for TTS delivery."""
        alerts = list(self.active_alerts)
        self.active_alerts.clear()
        return alerts

    def get_high_priority_alerts(self) -> list[dict]:
        """Return only high-priority alerts (security, safety) without clearing."""
        return [a for a in self.active_alerts if a["priority"] == "high"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _friendly_name(entity_id: str) -> str:
        """Convert entity_id to a human-friendly name."""
        # "binary_sensor.door_front" → "door front"
        # "light.kitchen" → "kitchen"
        _, _, name = entity_id.partition(".")
        return name.replace("_", " ")

    @staticmethod
    def _area_from_entity(entity_id: str) -> str:
        """Extract area slug from entity_id. e.g. light.kitchen → kitchen."""
        _, _, name = entity_id.partition(".")
        return name

    def _recent_motion_in_area(self, area: str, current_time: float, window_min: float = 60) -> bool:
        """Check if there was motion in the given area within the window."""
        last_motion = self._area_motion.get(area)
        if last_motion is None:
            return False
        return (current_time - last_motion) < (window_min * 60)

    @staticmethod
    def _is_numeric(s: str) -> bool:
        """Check if a string represents a valid number."""
        try:
            float(s)
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _hour_from_timestamp(ts: float) -> int:
        """Get hour (0-23) from a Unix timestamp (UTC)."""
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).hour

    # ------------------------------------------------------------------
    # Summary / debug
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return engine status for dashboard display."""
        return {
            "tracked_devices": len(self.device_states),
            "pending_alerts": len(self.active_alerts),
            "high_priority": len(self.get_high_priority_alerts()),
            "thresholds": dict(self.thresholds),
            "areas_with_motion": list(self._area_motion.keys()),
        }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    alerts = ProactiveAlerts()
    now = time.time()

    # Simulate front door opening 35 mins ago
    alerts.process_event(json.dumps({
        "entity_id": "binary_sensor.door_front",
        "state": "on",
        "timestamp": now - (35 * 60),
    }))

    # Simulate kitchen light on 65 mins ago (no motion)
    alerts.process_event(json.dumps({
        "entity_id": "light.kitchen",
        "state": "on",
        "timestamp": now - (65 * 60),
    }))

    # Simulate temperature drop
    alerts.process_event(json.dumps({
        "entity_id": "sensor.living_room_temperature",
        "state": "16.5",
        "timestamp": now,
    }))

    pending = alerts.get_pending_alerts()
    assert len(pending) == 3, f"Expected 3 alerts, got {len(pending)}"
    assert pending[0]["priority"] == "high"  # door = high priority
    assert pending[1]["priority"] == "low"   # light = low priority
    assert pending[2]["priority"] == "low"   # temp = low priority
    print(f"Smoke test passed: {len(pending)} alerts generated correctly.")
