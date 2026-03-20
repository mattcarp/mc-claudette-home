#!/usr/bin/env python3
"""
Tests for proactive_alerts.py — Claudette Home Proactive Alert Engine.

Tests all 6 alert rules:
  1. Door left open
  2. Window left open
  3. Lights on in empty room (motion-aware)
  4. Temperature out of comfort range (low + high)
  5. Unusual motion at odd hours
  6. Device failure (no response)

Plus: state management, priority levels, alert clearing, deduplication, edge cases.

Run:
    python3 -m pytest brain/test_proactive_alerts.py -v
"""

import datetime
import json
import os
import sys
import time
import unittest

# Add brain dir to path
sys.path.insert(0, os.path.dirname(__file__))
from proactive_alerts import ProactiveAlerts, DEFAULT_THRESHOLDS


def _event(entity_id: str, state: str, timestamp: float | None = None) -> str:
    """Helper: build a JSON event string."""
    evt = {"entity_id": entity_id, "state": state}
    if timestamp is not None:
        evt["timestamp"] = timestamp
    return json.dumps(evt)


class TestDoorAlerts(unittest.TestCase):
    """Rule 1: Door open > 30 min → high priority alert."""

    def test_door_open_triggers_after_threshold(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 35 * 60))
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "high")
        self.assertIn("door front", alerts[0]["message"])
        self.assertIn("35 minutes", alerts[0]["message"])

    def test_door_open_no_alert_before_threshold(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 20 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 0)

    def test_door_closed_no_alert(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "off", now - 60 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 0)

    def test_front_door_contact_sensor(self):
        """binary_sensor.front_door_contact should also trigger door rule."""
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.front_door_contact", "on", now - 35 * 60))
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "high")

    def test_custom_door_threshold(self):
        engine = ProactiveAlerts(thresholds={"door_open_min": 10})
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 15 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 1)


class TestWindowAlerts(unittest.TestCase):
    """Rule 2: Window open > 30 min → low priority alert."""

    def test_window_open_triggers(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.window_bedroom", "on", now - 35 * 60))
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "low")
        self.assertIn("window bedroom", alerts[0]["message"])

    def test_window_closed_no_alert(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.window_bedroom", "off", now - 60 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 0)


class TestLightAlerts(unittest.TestCase):
    """Rule 3: Light on in empty room > 60 min → low priority (motion-aware)."""

    def test_light_on_no_motion_triggers(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("light.kitchen", "on", now - 65 * 60))
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "low")
        self.assertIn("kitchen", alerts[0]["message"])
        self.assertIn("no motion", alerts[0]["message"])

    def test_light_on_with_recent_motion_no_alert(self):
        """If there's been recent motion in the room, don't alert."""
        engine = ProactiveAlerts()
        now = time.time()
        # Motion 10 minutes ago in kitchen
        engine.process_event(_event("binary_sensor.motion_kitchen", "on", now - 10 * 60))
        # Light on for 65 minutes
        engine.process_event(_event("light.kitchen", "on", now - 65 * 60))
        alerts = engine.get_pending_alerts()
        # Should have 0 light alerts (the motion sensor event itself might trigger
        # unusual-hours if we're in that window, but the light shouldn't alert)
        light_alerts = [a for a in alerts if a["entity"] == "light.kitchen"]
        self.assertEqual(len(light_alerts), 0)

    def test_light_on_less_than_threshold_no_alert(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("light.kitchen", "on", now - 30 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 0)

    def test_light_off_no_alert(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("light.kitchen", "off", now - 120 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 0)


class TestTemperatureAlerts(unittest.TestCase):
    """Rule 4: Temperature out of comfort range."""

    def test_cold_temperature_triggers(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("sensor.living_room_temperature", "15.5", now))
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("15.5°C", alerts[0]["message"])
        self.assertIn("Heating", alerts[0]["message"])

    def test_hot_temperature_triggers(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("sensor.living_room_temperature", "32.0", now))
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("32.0°C", alerts[0]["message"])
        self.assertIn("AC", alerts[0]["message"])

    def test_comfortable_temperature_no_alert(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("sensor.living_room_temperature", "22.0", now))
        self.assertEqual(len(engine.get_pending_alerts()), 0)

    def test_non_numeric_temperature_ignored(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("sensor.living_room_temperature", "unavailable", now))
        self.assertEqual(len(engine.get_pending_alerts()), 0)

    def test_boundary_temperature_no_alert(self):
        """Exactly at threshold = no alert (strict inequality)."""
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("sensor.living_room_temperature", "17.0", now))
        self.assertEqual(len(engine.get_pending_alerts()), 0)


class TestUnusualMotion(unittest.TestCase):
    """Rule 5: Motion at odd hours (1:00–5:00 UTC) → high priority."""

    def _ts_at_hour(self, hour: int) -> float:
        """Create a timestamp at a specific UTC hour today."""
        dt = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=hour, minute=30, second=0, microsecond=0
        )
        return dt.timestamp()

    def test_motion_at_2am_triggers(self):
        engine = ProactiveAlerts()
        ts = self._ts_at_hour(2)
        engine.process_event(_event("binary_sensor.motion_living_room", "on", ts))
        alerts = engine.get_pending_alerts()
        high = [a for a in alerts if a["priority"] == "high"]
        self.assertGreaterEqual(len(high), 1)
        self.assertIn("unusual hour", high[0]["message"])

    def test_motion_at_noon_no_alert(self):
        engine = ProactiveAlerts()
        ts = self._ts_at_hour(12)
        engine.process_event(_event("binary_sensor.motion_living_room", "on", ts))
        alerts = engine.get_pending_alerts()
        unusual = [a for a in alerts if "unusual hour" in a.get("message", "")]
        self.assertEqual(len(unusual), 0)

    def test_motion_off_no_alert(self):
        """Motion sensor going 'off' should not trigger unusual hours."""
        engine = ProactiveAlerts()
        ts = self._ts_at_hour(3)
        engine.process_event(_event("binary_sensor.motion_living_room", "off", ts))
        alerts = engine.get_pending_alerts()
        unusual = [a for a in alerts if "unusual hour" in a.get("message", "")]
        self.assertEqual(len(unusual), 0)


class TestDeviceFailure(unittest.TestCase):
    """Rule 6: Device not reporting → high priority."""

    def test_sensor_silent_triggers(self):
        engine = ProactiveAlerts(thresholds={"device_timeout_min": 5})
        now = time.time()
        past = now - 10 * 60
        # Sensor reports once, 10 min ago (eval_time = that past moment)
        engine.process_event(_event("sensor.living_room_temperature", "22.0", past), eval_time=past)
        # Now evaluate at current time — device hasn't reported since
        engine.evaluate_alerts(now)
        alerts = engine.get_pending_alerts()
        self.assertGreaterEqual(len(alerts), 1)
        failure_alerts = [a for a in alerts if "responded" in a["message"]]
        self.assertEqual(len(failure_alerts), 1)
        self.assertEqual(failure_alerts[0]["priority"], "high")

    def test_active_sensor_no_failure(self):
        engine = ProactiveAlerts(thresholds={"device_timeout_min": 5})
        now = time.time()
        engine.process_event(_event("sensor.living_room_temperature", "22.0", now), eval_time=now)
        engine.evaluate_alerts(now)
        alerts = engine.get_pending_alerts()
        failure_alerts = [a for a in alerts if "responded" in a.get("message", "")]
        self.assertEqual(len(failure_alerts), 0)


class TestStateManagement(unittest.TestCase):
    """State tracking, deduplication, and alert clearing."""

    def test_no_duplicate_alerts(self):
        """Same event processed twice should not produce two alerts."""
        engine = ProactiveAlerts()
        now = time.time()
        evt = _event("binary_sensor.door_front", "on", now - 35 * 60)
        engine.process_event(evt)
        engine.process_event(evt)  # Same event again
        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 1)

    def test_state_change_resets_alert(self):
        """Closing and re-opening a door should allow a new alert."""
        # Use high device_timeout so failure detection doesn't interfere
        engine = ProactiveAlerts(thresholds={"device_timeout_min": 120})
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 35 * 60))
        self.assertEqual(len(engine.get_pending_alerts()), 1)

        # Close the door
        engine.process_event(_event("binary_sensor.door_front", "off", now))
        # Re-open for another 35 min
        engine.process_event(_event("binary_sensor.door_front", "on", now))
        # Not enough time yet
        self.assertEqual(len(engine.get_pending_alerts()), 0)
        # Manually eval later
        engine.evaluate_alerts(now + 36 * 60)
        alerts = engine.get_pending_alerts()
        door_alerts = [a for a in alerts if "open" in a["message"]]
        self.assertEqual(len(door_alerts), 1)

    def test_get_pending_clears(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 35 * 60))
        first = engine.get_pending_alerts()
        self.assertEqual(len(first), 1)
        second = engine.get_pending_alerts()
        self.assertEqual(len(second), 0)

    def test_get_high_priority_does_not_clear(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 35 * 60))
        high = engine.get_high_priority_alerts()
        self.assertEqual(len(high), 1)
        # Still there
        self.assertEqual(len(engine.active_alerts), 1)

    def test_invalid_json_ignored(self):
        engine = ProactiveAlerts()
        engine.process_event("not valid json {{{")
        self.assertEqual(len(engine.get_pending_alerts()), 0)

    def test_missing_entity_id_ignored(self):
        engine = ProactiveAlerts()
        engine.process_event(json.dumps({"state": "on"}))
        self.assertEqual(len(engine.get_pending_alerts()), 0)


class TestStatus(unittest.TestCase):
    """Engine status/debug endpoint."""

    def test_status_empty(self):
        engine = ProactiveAlerts()
        s = engine.status()
        self.assertEqual(s["tracked_devices"], 0)
        self.assertEqual(s["pending_alerts"], 0)

    def test_status_after_events(self):
        engine = ProactiveAlerts()
        now = time.time()
        engine.process_event(_event("binary_sensor.door_front", "on", now - 35 * 60))
        engine.process_event(_event("binary_sensor.motion_kitchen", "on", now))
        s = engine.status()
        self.assertGreaterEqual(s["tracked_devices"], 2)
        self.assertIn("kitchen", s["areas_with_motion"])


class TestMultipleAlerts(unittest.TestCase):
    """Integration: multiple rules firing together."""

    def test_three_alerts_smoke_test(self):
        """Reproduce the original smoke test from __main__."""
        engine = ProactiveAlerts()
        now = time.time()

        engine.process_event(_event("binary_sensor.door_front", "on", now - 35 * 60))
        engine.process_event(_event("light.kitchen", "on", now - 65 * 60))
        engine.process_event(_event("sensor.living_room_temperature", "16.5", now))

        alerts = engine.get_pending_alerts()
        self.assertEqual(len(alerts), 3)
        self.assertEqual(alerts[0]["priority"], "high")   # door
        self.assertEqual(alerts[1]["priority"], "low")     # light
        self.assertEqual(alerts[2]["priority"], "low")     # temp

    def test_mixed_priorities(self):
        """Mix of high and low priority alerts."""
        engine = ProactiveAlerts()
        now = time.time()

        # High: door open
        engine.process_event(_event("binary_sensor.door_front", "on", now - 40 * 60))
        # Low: window open
        engine.process_event(_event("binary_sensor.window_bedroom", "on", now - 40 * 60))
        # Low: temperature
        engine.process_event(_event("sensor.kitchen_temperature", "32.5", now))

        alerts = engine.get_pending_alerts()
        high = [a for a in alerts if a["priority"] == "high"]
        low = [a for a in alerts if a["priority"] == "low"]
        self.assertEqual(len(high), 1)
        self.assertEqual(len(low), 2)


# Allow running: python3 brain/test_proactive_alerts.py
if __name__ == "__main__":
    unittest.main()
