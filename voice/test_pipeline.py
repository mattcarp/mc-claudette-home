#!/usr/bin/env python3
"""
Claudette Home — Pipeline Tests
Tests for pipeline.py and ha_bridge.py (no live HA or API required).

Run:
  python3 voice/test_pipeline.py
  python3 -m pytest voice/test_pipeline.py -v
"""

import io
import json
import sys
import os
import wave

# Path setup
VOICE_DIR = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(VOICE_DIR, "intent_parser"))
sys.path.insert(0, os.path.join(VOICE_DIR, "ha_bridge"))
sys.path.insert(0, VOICE_DIR)

import pytest


# ---------------------------------------------------------------------------
# HA Bridge stub tests
# ---------------------------------------------------------------------------

class TestHABridgeStub:
    def setup_method(self):
        from ha_bridge import get_bridge
        self.bridge = get_bridge(stub=True)

    def test_ping(self):
        assert self.bridge.ping() is True

    def test_get_entities_structure(self):
        entities = self.bridge.get_entities()
        assert "lights" in entities
        assert "switches" in entities
        assert "scenes" in entities
        assert "locks" in entities
        # All lights should have entity_id and name
        for light in entities["lights"]:
            assert "entity_id" in light
            assert "name" in light

    def test_get_entities_non_empty(self):
        entities = self.bridge.get_entities()
        total = sum(len(v) for v in entities.values())
        assert total > 10, f"Expected >10 entities, got {total}"

    def test_execute_single_call_service(self):
        action = {
            "action": "call_service",
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.living_room",
            "params": {"brightness_pct": 50},
        }
        results = self.bridge.execute_action(action)
        assert len(results) == 1
        assert results[0]["ok"] is True

    def test_execute_single_query(self):
        action = {
            "action": "query",
            "entity_id": "sensor.living_room_temperature",
        }
        results = self.bridge.execute_action(action)
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert results[0]["action"] == "query"

    def test_execute_single_clarify(self):
        action = {
            "action": "clarify",
            "question": "Which room?",
        }
        results = self.bridge.execute_action(action)
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert results[0]["question"] == "Which room?"

    def test_execute_multi_action(self):
        actions = [
            {"action": "call_service", "domain": "light", "service": "turn_off",
             "entity_id": "light.living_room"},
            {"action": "call_service", "domain": "lock", "service": "lock",
             "entity_id": "lock.front_door"},
        ]
        results = self.bridge.execute_action(actions)
        assert len(results) == 2
        assert all(r["ok"] for r in results)

    def test_get_state_stub(self):
        state = self.bridge.get_state("light.living_room")
        assert state["entity_id"] == "light.living_room"
        assert "state" in state


# ---------------------------------------------------------------------------
# Pipeline response builder tests
# ---------------------------------------------------------------------------

class TestBuildResponse:
    def setup_method(self):
        from pipeline import build_response
        self.build_response = build_response

    def test_turn_on_response(self):
        action = {"action": "call_service", "domain": "light",
                  "service": "turn_on", "entity_id": "light.living_room"}
        r = self.build_response(action, [{"ok": True}])
        assert "on" in r.lower()
        assert "living room" in r.lower()

    def test_turn_off_response(self):
        action = {"action": "call_service", "domain": "light",
                  "service": "turn_off", "entity_id": "light.kitchen"}
        r = self.build_response(action, [{"ok": True}])
        assert "off" in r.lower()
        assert "kitchen" in r.lower()

    def test_scene_response(self):
        action = {"action": "call_service", "domain": "scene",
                  "service": "activate", "entity_id": "scene.goodnight"}
        r = self.build_response(action, [{"ok": True}])
        assert "goodnight" in r.lower() or "scene" in r.lower()

    def test_lock_response(self):
        action = {"action": "call_service", "domain": "lock",
                  "service": "lock", "entity_id": "lock.front_door"}
        r = self.build_response(action, [{"ok": True}])
        assert "lock" in r.lower()

    def test_query_response(self):
        action = {"action": "query", "entity_id": "sensor.living_room_temperature"}
        r = self.build_response(action, [{"ok": True, "state": "22.5"}])
        assert "22.5" in r

    def test_query_failure_response(self):
        action = {"action": "query", "entity_id": "sensor.living_room_temperature"}
        r = self.build_response(action, [{"ok": False}])
        assert "couldn't" in r.lower() or "reading" in r.lower()

    def test_clarify_response(self):
        action = {"action": "clarify", "question": "Which room did you mean?"}
        r = self.build_response(action, [])
        assert r == "Which room did you mean?"

    def test_temperature_response(self):
        action = {"action": "call_service", "domain": "climate",
                  "service": "set_temperature", "entity_id": "climate.thermostat",
                  "params": {"temperature": 22}}
        r = self.build_response(action, [{"ok": True}])
        assert "22" in r

    def test_multi_action_response(self):
        actions = [
            {"action": "call_service", "domain": "light", "service": "turn_off", "entity_id": "light.living_room"},
            {"action": "call_service", "domain": "lock", "service": "lock", "entity_id": "lock.front_door"},
        ]
        r = self.build_response(actions, [{}, {}])
        assert "2" in r or "two" in r.lower() or "done" in r.lower()

    def test_toggle_response(self):
        action = {"action": "call_service", "domain": "switch",
                  "service": "toggle", "entity_id": "switch.tv"}
        r = self.build_response(action, [{"ok": True}])
        assert "toggled" in r.lower() or "tv" in r.lower()

    def test_shutters_close_response(self):
        action = {"action": "call_service", "domain": "cover",
                  "service": "close", "entity_id": "cover.living_room_shutters"}
        r = self.build_response(action, [{"ok": True}])
        assert "shutter" in r.lower() or "close" in r.lower()


# ---------------------------------------------------------------------------
# Audio recording stub test
# ---------------------------------------------------------------------------

class TestAudioStub:
    def test_record_audio_stub_returns_wav(self):
        from pipeline import record_audio_stub
        wav_bytes = record_audio_stub(seconds=1)
        assert len(wav_bytes) > 44  # WAV header is 44 bytes minimum
        # Verify it's a valid WAV
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getsampwidth() == 2  # 16-bit


# ---------------------------------------------------------------------------
# HA bridge error handling tests
# ---------------------------------------------------------------------------

class TestHABridgeLiveErrors:
    """Test live bridge error paths without a real HA instance."""

    def test_no_token_raises(self):
        from ha_bridge import HABridge
        # Patch the env to clear HA_TOKEN
        orig = os.environ.pop("HA_TOKEN", None)
        try:
            import pytest
            with pytest.raises(EnvironmentError, match="HA_TOKEN"):
                HABridge(token="")
        finally:
            if orig:
                os.environ["HA_TOKEN"] = orig

    def test_connection_error_raises_ha_error(self):
        from ha_bridge import HABridge, HAError
        bridge = HABridge(url="http://127.0.0.1:19999", token="fake_token")
        with pytest.raises(HAError, match="connection error"):
            bridge.get_config()

    def test_ping_returns_false_on_connection_error(self):
        from ha_bridge import HABridge
        bridge = HABridge(url="http://127.0.0.1:19999", token="fake_token")
        assert bridge.ping() is False


# ---------------------------------------------------------------------------
# Service file generation
# ---------------------------------------------------------------------------

class TestServiceFile:
    def test_service_file_content(self):
        from pipeline import SERVICE_UNIT
        assert "Claudette Home" in SERVICE_UNIT
        assert "wake_word_bridge.py" in SERVICE_UNIT
        assert "pipeline.py" in SERVICE_UNIT
        assert "EnvironmentFile=/etc/environment" in SERVICE_UNIT
        assert "Restart=on-failure" in SERVICE_UNIT


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run without pytest if available
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
    except ImportError:
        print("pytest not installed — running basic assertions")
        t = TestHABridgeStub()
        t.setup_method()
        t.test_ping()
        t.test_get_entities_structure()
        t.test_get_entities_non_empty()
        t.test_execute_single_call_service()
        t.test_execute_single_query()
        t.test_execute_single_clarify()
        t.test_execute_multi_action()

        t2 = TestBuildResponse()
        t2.setup_method()
        t2.test_turn_on_response()
        t2.test_turn_off_response()
        t2.test_scene_response()
        t2.test_lock_response()
        t2.test_query_response()
        t2.test_clarify_response()

        t3 = TestAudioStub()
        t3.test_record_audio_stub_returns_wav()

        print("All basic assertions passed!")
