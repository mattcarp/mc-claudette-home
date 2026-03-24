#!/usr/bin/env python3
"""
Tests: Alert Pipeline Integration in voice/pipeline.py

Verifies that the proactive alert system is properly wired into the
main voice pipeline event loop:
- state_changed events are fed to the alert engine
- wake_word_detected events trigger batch delivery
- _init_alert_integration returns a working instance
- pipeline handles events with and without alert integration
"""

import datetime
import io
import json
import os
import sys
import time
import unittest

# Ensure project root and voice dirs are on path
VOICE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(VOICE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if VOICE_DIR not in sys.path:
    sys.path.insert(0, VOICE_DIR)
sys.path.insert(0, os.path.join(VOICE_DIR, "ha_bridge"))
sys.path.insert(0, os.path.join(VOICE_DIR, "intent_parser"))


class TestInitAlertIntegration(unittest.TestCase):
    """Test _init_alert_integration creates a working integration."""

    def test_returns_integration_object(self):
        from pipeline import _init_alert_integration
        integration = _init_alert_integration()
        self.assertIsNotNone(integration)

    def test_integration_has_engine(self):
        from pipeline import _init_alert_integration
        integration = _init_alert_integration()
        self.assertIsNotNone(integration.engine)

    def test_integration_has_router(self):
        from pipeline import _init_alert_integration
        integration = _init_alert_integration()
        self.assertIsNotNone(integration.router)

    def test_integration_status(self):
        from pipeline import _init_alert_integration
        integration = _init_alert_integration()
        status = integration.status()
        self.assertIn("engine", status)
        self.assertIn("delivery", status)


class TestStateChangedRouting(unittest.TestCase):
    """Test that state_changed events are correctly routed to the alert engine."""

    def setUp(self):
        from brain.alert_delivery import AlertPipelineIntegration
        self.delivered = []

        def capture(event_json):
            self.delivered.append(json.loads(event_json))

        self.integration = AlertPipelineIntegration(output_fn=capture)

    def _make_state_event(self, entity_id, state, minutes_ago=40):
        """Create an HA state_changed event JSON string.
        
        The proactive_alerts engine uses `timestamp` (epoch float) for when
        the state changed, and eval_time (also epoch) for when to evaluate.
        To simulate a door open for N minutes, we set timestamp = now - N min
        and pass eval_time = now.
        """
        ts = time.time() - (minutes_ago * 60)
        return json.dumps({
            "type": "state_changed",
            "entity_id": entity_id,
            "state": state,
            "timestamp": ts,
        })

    def test_door_open_triggers_high_priority_alert(self):
        """Door open for 40 min → high priority → immediate delivery."""
        event = self._make_state_event("binary_sensor.front_door", "on", minutes_ago=40)
        modes = self.integration.on_ha_event(event)
        self.assertIn("immediate", modes)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0]["priority"], "high")

    def test_temperature_low_triggers_low_priority_alert(self):
        """Temperature drop → low priority → batched."""
        event = self._make_state_event("sensor.living_room_temperature", "15.5", minutes_ago=5)
        modes = self.integration.on_ha_event(event)
        self.assertIn("batched", modes)
        self.assertEqual(len(self.delivered), 0)  # Not immediately delivered

    def test_normal_state_no_alert(self):
        """Normal temperature → no alert generated."""
        event = self._make_state_event("sensor.living_room_temperature", "22.0", minutes_ago=5)
        modes = self.integration.on_ha_event(event)
        self.assertEqual(len(modes), 0)  # No alerts

    def test_light_on_with_no_motion_triggers_alert(self):
        """Light on for >60 min with no motion → low priority alert."""
        event = self._make_state_event("light.kitchen", "on", minutes_ago=70)
        modes = self.integration.on_ha_event(event)
        self.assertIn("batched", modes)

    def test_door_closed_no_alert(self):
        """Door closed → no alert."""
        event = self._make_state_event("binary_sensor.front_door", "off", minutes_ago=40)
        modes = self.integration.on_ha_event(event)
        self.assertEqual(len(modes), 0)


class TestConversationBatchDelivery(unittest.TestCase):
    """Test that batched alerts are delivered at conversation start."""

    def setUp(self):
        from brain.alert_delivery import AlertPipelineIntegration
        self.delivered = []

        def capture(event_json):
            self.delivered.append(json.loads(event_json))

        self.integration = AlertPipelineIntegration(output_fn=capture)

    def test_batch_delivery_on_conversation_start(self):
        """Batched alerts are delivered when a conversation starts."""
        # Feed a low-priority alert (gets batched)
        event = json.dumps({
            "type": "state_changed",
            "entity_id": "sensor.living_room_temperature",
            "state": "15.0",
            "timestamp": time.time() - 300,  # 5 min ago
        })
        self.integration.on_ha_event(event)
        self.assertEqual(len(self.delivered), 0)

        # Now start a conversation → should deliver the batch
        count = self.integration.on_conversation_start()
        self.assertEqual(count, 1)
        self.assertEqual(len(self.delivered), 1)

    def test_no_batch_when_empty(self):
        """No delivery when batch is empty."""
        count = self.integration.on_conversation_start()
        self.assertEqual(count, 0)
        self.assertEqual(len(self.delivered), 0)

    def test_multiple_batched_alerts_combined(self):
        """Multiple low-priority alerts are combined in batch delivery."""
        now = time.time()
        ts = now - 300  # 5 min ago

        self.integration.on_ha_event(json.dumps({
            "type": "state_changed",
            "entity_id": "sensor.living_room_temperature",
            "state": "15.0",
            "timestamp": ts,
        }), eval_time=now)

        self.integration.on_ha_event(json.dumps({
            "type": "state_changed",
            "entity_id": "sensor.bedroom_temperature",
            "state": "14.5",
            "timestamp": ts,
        }), eval_time=now)

        count = self.integration.on_conversation_start()
        self.assertEqual(count, 2)
        self.assertEqual(len(self.delivered), 1)  # Combined into one TTS event
        self.assertIn("A few things", self.delivered[0]["text"])


class TestPipelineEventHandling(unittest.TestCase):
    """Test that pipeline.py handles mixed event types correctly."""

    def test_state_changed_event_parsed(self):
        """Verify a state_changed JSON line is valid for the integration."""
        event = {
            "type": "state_changed",
            "entity_id": "binary_sensor.front_door",
            "state": "on",
            "last_changed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        line = json.dumps(event)
        parsed = json.loads(line)
        self.assertEqual(parsed["type"], "state_changed")
        self.assertEqual(parsed["entity_id"], "binary_sensor.front_door")

    def test_non_state_events_ignored_by_integration(self):
        """Non-state_changed events don't produce alerts."""
        from brain.alert_delivery import AlertPipelineIntegration
        delivered = []
        integration = AlertPipelineIntegration(output_fn=lambda x: delivered.append(x))

        # Feed a non-state event — should not crash, should produce no alerts
        result = integration.on_ha_event(json.dumps({
            "type": "something_else",
            "data": "foo",
        }))
        self.assertEqual(len(result), 0)
        self.assertEqual(len(delivered), 0)

    def test_malformed_event_handled_gracefully(self):
        """Malformed JSON in event doesn't crash the integration."""
        from brain.alert_delivery import AlertPipelineIntegration
        integration = AlertPipelineIntegration()

        # Missing entity_id — should not crash
        result = integration.on_ha_event(json.dumps({
            "type": "state_changed",
            "state": "on",
        }))
        # No crash, returns empty or handles gracefully
        self.assertIsInstance(result, list)


class TestCombinedStatus(unittest.TestCase):
    """Test the combined engine + router status endpoint."""

    def test_status_after_mixed_events(self):
        """Status reflects both engine and router state after events."""
        from brain.alert_delivery import AlertPipelineIntegration
        delivered = []
        integration = AlertPipelineIntegration(output_fn=lambda x: delivered.append(x))

        # Feed a high-priority alert — door open 40 min ago
        now = time.time()
        integration.on_ha_event(json.dumps({
            "type": "state_changed",
            "entity_id": "binary_sensor.front_door",
            "state": "on",
            "timestamp": now - (40 * 60),
        }), eval_time=now)

        status = integration.status()
        self.assertIn("engine", status)
        self.assertIn("delivery", status)
        self.assertGreaterEqual(status["delivery"]["stats"]["total_received"], 1)
        self.assertGreaterEqual(status["delivery"]["stats"]["immediate_delivered"], 1)


if __name__ == "__main__":
    unittest.main()
