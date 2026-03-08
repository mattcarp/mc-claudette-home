#!/usr/bin/env python3
"""
Tests for whole_home_audio.py — all run in stub mode (no HA required).

Run:
    python3 -m pytest brain/test_whole_home_audio.py -v
    # or:
    python3 brain/test_whole_home_audio.py
"""

import sys
import os
import unittest

# Add brain dir to path
sys.path.insert(0, os.path.dirname(__file__))
from whole_home_audio import (
    AudioControllerStub,
    get_controller,
    ZONE_ENTITIES,
    DUCK_LEVEL,
    RESTORE_LEVEL,
)


class TestZoneResolution(unittest.TestCase):
    """Zone name → entity ID mapping."""

    def test_whole_house_aliases(self):
        for alias in ("whole_house", "whole house", "everywhere", "all"):
            self.assertEqual(ZONE_ENTITIES.get(alias), "media_player.whole_house")

    def test_wiim_aliases(self):
        for alias in ("wiim", "hi-fi", "hifi"):
            self.assertEqual(ZONE_ENTITIES.get(alias), "media_player.wiim_mini")

    def test_room_zones(self):
        self.assertEqual(ZONE_ENTITIES.get("kitchen"), "media_player.echo_dot_kitchen")
        self.assertEqual(ZONE_ENTITIES.get("living room"), "media_player.echo_dot_living_room")
        self.assertEqual(ZONE_ENTITIES.get("master bedroom"), "media_player.echo_dot_bedroom")


class TestStubAnnounce(unittest.TestCase):
    """TTS announcement via stub."""

    def setUp(self):
        self.ctrl = AudioControllerStub()

    def test_announce_default_zone(self):
        result = self.ctrl.announce("Dinner is ready")
        self.assertEqual(result["action"], "announce")
        self.assertEqual(result["message"], "Dinner is ready")
        self.assertEqual(result["zone"], "whole_house")

    def test_announce_kitchen_zone(self):
        result = self.ctrl.announce("Your toast is burning", zone="kitchen")
        self.assertEqual(result["zone"], "kitchen")

    def test_announce_logged(self):
        self.ctrl.announce("Hello")
        self.assertEqual(len(self.ctrl.calls), 1)
        self.assertEqual(self.ctrl.calls[0]["action"], "announce")


class TestStubDoorbellAnnounce(unittest.TestCase):
    """Doorbell duck-and-announce."""

    def setUp(self):
        self.ctrl = AudioControllerStub()

    def test_doorbell_default_message(self):
        result = self.ctrl.doorbell_announce()
        self.assertIn("doorbell", result["action"])
        self.assertIn("front door", result["message"].lower())

    def test_doorbell_custom_message(self):
        result = self.ctrl.doorbell_announce(message="Package delivery")
        self.assertEqual(result["message"], "Package delivery")

    def test_doorbell_duck_level(self):
        result = self.ctrl.doorbell_announce(duck_level=0.1, restore_level=0.5)
        self.assertEqual(result["duck_level"], 0.1)
        self.assertEqual(result["restore_level"], 0.5)

    def test_doorbell_volume_restored(self):
        """Volume should be at restore_level after doorbell sequence."""
        self.ctrl.doorbell_announce(duck_level=0.1, restore_level=0.45)
        # Stub tracks volume changes
        self.assertEqual(self.ctrl._volumes.get("whole_house"), 0.45)

    def test_doorbell_volume_was_ducked(self):
        """Volume should have been ducked (pre-duck saved)."""
        self.ctrl.doorbell_announce(duck_level=0.1)
        self.assertIn("whole_house_pre_duck", self.ctrl._volumes)


class TestStubVolumeControl(unittest.TestCase):
    """Volume set operations."""

    def setUp(self):
        self.ctrl = AudioControllerStub()

    def test_set_volume(self):
        result = self.ctrl.set_volume("whole_house", 0.5)
        self.assertEqual(result["action"], "set_volume")
        self.assertEqual(result["level"], 0.5)
        self.assertEqual(result["entity"], "media_player.whole_house")

    def test_set_volume_kitchen(self):
        result = self.ctrl.set_volume("kitchen", 0.3)
        self.assertEqual(result["entity"], "media_player.echo_dot_kitchen")

    def test_volume_stored(self):
        self.ctrl.set_volume("kitchen", 0.7)
        self.assertEqual(self.ctrl._volumes.get("media_player.echo_dot_kitchen"), 0.7)


class TestStubPlayback(unittest.TestCase):
    """Play/pause/stop operations."""

    def setUp(self):
        self.ctrl = AudioControllerStub()

    def test_play(self):
        result = self.ctrl.play("whole_house")
        self.assertEqual(result["action"], "play")
        self.assertEqual(result["entity"], "media_player.whole_house")

    def test_play_with_source(self):
        result = self.ctrl.play("whole_house", source="spotify")
        self.assertEqual(result["source"], "spotify")

    def test_pause(self):
        self.ctrl.play("whole_house")
        result = self.ctrl.pause("whole_house")
        self.assertEqual(result["action"], "pause")
        self.assertFalse(self.ctrl._playing.get("media_player.whole_house"))

    def test_stop(self):
        result = self.ctrl.stop("kitchen")
        self.assertEqual(result["action"], "stop")
        self.assertFalse(self.ctrl._playing.get("media_player.echo_dot_kitchen"))

    def test_play_state_tracked(self):
        self.ctrl.play("whole_house")
        self.assertTrue(self.ctrl._playing.get("media_player.whole_house"))


class TestStubStatus(unittest.TestCase):
    """Status reporting."""

    def setUp(self):
        self.ctrl = AudioControllerStub()

    def test_status_idle_by_default(self):
        status = self.ctrl.status("whole_house")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["entity"], "media_player.whole_house")

    def test_status_playing_after_play(self):
        self.ctrl.play("whole_house")
        status = self.ctrl.status("whole_house")
        self.assertEqual(status["state"], "playing")

    def test_status_is_stub(self):
        status = self.ctrl.status()
        self.assertTrue(status.get("stub"))


class TestIntentRouter(unittest.TestCase):
    """execute_intent routing from intent parser output."""

    def setUp(self):
        self.ctrl = AudioControllerStub()

    def test_route_announce(self):
        intent = {"action": "announce", "message": "Welcome home", "zone": "whole_house"}
        result = self.ctrl.execute_intent(intent)
        self.assertIsNotNone(result)
        self.assertEqual(len(self.ctrl.calls), 1)

    def test_route_doorbell_announce(self):
        intent = {"action": "doorbell_announce", "message": "Front door"}
        result = self.ctrl.execute_intent(intent)
        self.assertIsNotNone(result)

    def test_route_unknown_action(self):
        intent = {"action": "fly_to_the_moon"}
        result = self.ctrl.execute_intent(intent)
        # Should return gracefully with logged entry
        self.assertEqual(len(self.ctrl.calls), 1)


class TestGetController(unittest.TestCase):
    """Factory function."""

    def test_stub_returns_stub(self):
        ctrl = get_controller(stub=True)
        self.assertIsInstance(ctrl, AudioControllerStub)

    def test_real_needs_token(self):
        """Real controller should raise if HA_TOKEN not set."""
        # Temporarily clear HA_TOKEN
        old_token = os.environ.pop("HA_TOKEN", None)
        try:
            with self.assertRaises(EnvironmentError):
                get_controller(stub=False)
        finally:
            if old_token:
                os.environ["HA_TOKEN"] = old_token


class TestDefaultConstants(unittest.TestCase):
    """Sanity checks on default config values."""

    def test_duck_level_is_low(self):
        self.assertLess(DUCK_LEVEL, 0.3)

    def test_restore_level_is_reasonable(self):
        self.assertGreater(RESTORE_LEVEL, 0.2)
        self.assertLess(RESTORE_LEVEL, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
