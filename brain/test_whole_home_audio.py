#!/usr/bin/env python3
"""
Tests for whole_home_audio.py — all run in stub mode (no HA required).

Run:
    python3 -m pytest brain/test_whole_home_audio.py -v
    # or:
    python3 brain/test_whole_home_audio.py
"""

import io
import sys
import os
import unittest
from contextlib import redirect_stdout

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

    def test_next_track(self):
        result = self.ctrl.next_track("whole_house")
        self.assertEqual(result["action"], "next_track")
        self.assertEqual(result["entity"], "media_player.whole_house")
        self.assertTrue(self.ctrl._playing.get("media_player.whole_house"))

    def test_previous_track(self):
        result = self.ctrl.previous_track("kitchen")
        self.assertEqual(result["action"], "previous_track")
        self.assertEqual(result["entity"], "media_player.echo_dot_kitchen")
        self.assertTrue(self.ctrl._playing.get("media_player.echo_dot_kitchen"))

    def test_volume_up_increments(self):
        self.ctrl.set_volume("whole_house", 0.4)
        result = self.ctrl.volume_up("whole_house")
        self.assertEqual(result["action"], "volume_up")
        self.assertEqual(result["level"], 0.5)
        self.assertEqual(self.ctrl._volumes.get("media_player.whole_house"), 0.5)

    def test_volume_down_decrements(self):
        self.ctrl.set_volume("kitchen", 0.3)
        result = self.ctrl.volume_down("kitchen")
        self.assertEqual(result["action"], "volume_down")
        self.assertEqual(result["level"], 0.2)
        self.assertEqual(self.ctrl._volumes.get("media_player.echo_dot_kitchen"), 0.2)

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


    def test_route_play_intent(self):
        intent = {"action": "play", "zone": "kitchen", "source": "spotify"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "play")
        self.assertEqual(result["zone"], "kitchen")
        self.assertEqual(result["source"], "spotify")

    def test_route_pause_intent(self):
        self.ctrl.play("whole_house")
        intent = {"action": "pause", "zone": "whole_house"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "pause")
        self.assertFalse(self.ctrl._playing.get("media_player.whole_house"))

    def test_route_stop_intent(self):
        self.ctrl.play("kitchen")
        intent = {"action": "stop", "zone": "kitchen"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "stop")
        self.assertFalse(self.ctrl._playing.get("media_player.echo_dot_kitchen"))

    def test_route_next_track_intent(self):
        intent = {"action": "next_track", "zone": "whole_house"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "next_track")
        self.assertEqual(result["entity"], "media_player.whole_house")

    def test_route_previous_track_intent(self):
        intent = {"action": "previous_track", "zone": "kitchen"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "previous_track")
        self.assertEqual(result["entity"], "media_player.echo_dot_kitchen")

    def test_route_volume_intent(self):
        intent = {"action": "volume", "zone": "bedroom", "level": 0.25}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "set_volume")
        self.assertEqual(result["entity"], "media_player.echo_dot_bedroom")
        self.assertEqual(result["level"], 0.25)

    def test_route_volume_up_intent(self):
        self.ctrl.set_volume("bedroom", 0.2)
        intent = {"action": "volume_up", "zone": "bedroom"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "volume_up")
        self.assertEqual(result["level"], 0.3)

    def test_route_volume_down_intent(self):
        self.ctrl.set_volume("bedroom", 0.2)
        intent = {"action": "volume_down", "zone": "bedroom"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["action"], "volume_down")
        self.assertEqual(result["level"], 0.1)

    def test_route_status_intent(self):
        self.ctrl.play("whole_house")
        intent = {"action": "status", "zone": "whole_house"}
        result = self.ctrl.execute_intent(intent)
        self.assertEqual(result["state"], "playing")
        self.assertEqual(result["entity"], "media_player.whole_house")

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


class TestEntityDiscovery(unittest.TestCase):
    """Tests for HA entity auto-discovery (sync_entities)."""

    def test_sync_mentioned_in_module_doc(self):
        """The 'sync' action should be documented in the module docstring."""
        import whole_home_audio
        self.assertIn("sync", whole_home_audio.__doc__)

    def test_sync_entities_on_real_controller_class(self):
        """
        sync_entities and print_entity_discovery are on AudioController (real HA),
        NOT on the stub (which has no network access). Verify both exist on the class.
        """
        import whole_home_audio as wha
        # Real controller class has the methods
        self.assertTrue(hasattr(wha.AudioController, "sync_entities"))
        self.assertTrue(callable(wha.AudioController.sync_entities))
        self.assertTrue(hasattr(wha.AudioController, "print_entity_discovery"))
        self.assertTrue(callable(wha.AudioController.print_entity_discovery))
        # Stub does NOT have sync_entities (needs real HA connection)
        self.assertFalse(hasattr(wha.AudioControllerStub, "sync_entities"))


class TestSyncEntitiesLogic(unittest.TestCase):
    """Test the real discovery helper methods without needing a live HA box."""

    def test_group_detection_handles_friendly_names_not_just_exact_ids(self):
        from whole_home_audio import AudioController

        self.assertTrue(
            AudioController._is_group_entity(
                "media_player.family_audio",
                "Whole House Audio",
            )
        )
        self.assertTrue(
            AudioController._is_group_entity(
                "media_player.ground_floor_players",
                "Downstairs Speakers",
            )
        )
        self.assertFalse(
            AudioController._is_group_entity(
                "media_player.echo_pop_office",
                "Office Echo",
            )
        )

    def test_echo_detection_handles_echo_pop_and_alexa_media_names(self):
        from whole_home_audio import AudioController

        self.assertTrue(
            AudioController._is_echo_entity(
                "media_player.echo_pop_office",
                "Office Echo Pop",
            )
        )
        self.assertTrue(
            AudioController._is_echo_entity(
                "media_player.alexa_media_kitchen",
                "Kitchen Alexa",
            )
        )
        self.assertFalse(
            AudioController._is_echo_entity(
                "media_player.wiim_mini",
                "WiiM Mini",
            )
        )

    def test_zone_aliases_can_be_derived_from_nonstandard_echo_entity_names(self):
        from whole_home_audio import AudioController

        aliases = AudioController._zone_aliases_for_echo(
            "media_player.alexa_media_living_room",
            "Living Room Alexa",
        )
        self.assertIn("living_room", aliases)
        self.assertIn("living room", aliases)

    def test_zone_aliases_fall_back_to_friendly_name_when_entity_name_is_generic(self):
        from whole_home_audio import AudioController

        aliases = AudioController._zone_aliases_for_echo(
            "media_player.media_player_2",
            "Kitchen Echo Dot",
        )
        self.assertIn("kitchen", aliases)

    def test_suggested_zones_include_detected_group_aliases(self):
        from whole_home_audio import AudioController

        aliases = AudioController._zone_aliases_for_group(
            "media_player.family_audio",
            "Whole House Audio",
        )
        self.assertIn("whole_house", aliases)
        self.assertIn("whole house", aliases)

    def test_print_entity_discovery_outputs_usable_zone_mapping_for_live_setup(self):
        from whole_home_audio import AudioController

        ctrl = object.__new__(AudioController)
        ctrl.sync_entities = lambda: {
            "total": 3,
            "groups": [
                {
                    "entity_id": "media_player.family_audio",
                    "friendly_name": "Whole House Audio",
                    "state": "idle",
                }
            ],
            "wiim": [
                {
                    "entity_id": "media_player.wiim_amp_living_room",
                    "friendly_name": "Living Room WiiM Amp",
                    "state": "playing",
                }
            ],
            "echos": [
                {
                    "entity_id": "media_player.media_player_2",
                    "friendly_name": "Kitchen Echo Dot",
                    "state": "standby",
                }
            ],
            "others": [],
            "suggested_zones": {
                "whole_house": "media_player.family_audio",
                "whole house": "media_player.family_audio",
                "kitchen": "media_player.media_player_2",
                "wiim": "media_player.wiim_amp_living_room",
            },
        }

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            ctrl.print_entity_discovery()

        rendered = stdout.getvalue()
        self.assertIn("Entity Discovery Report", rendered)
        self.assertIn("media_player.family_audio", rendered)
        self.assertIn("Kitchen Echo Dot", rendered)
        self.assertIn("'whole_house': 'media_player.family_audio'", rendered)
        self.assertIn("'kitchen': 'media_player.media_player_2'", rendered)
        self.assertIn("'wiim': 'media_player.wiim_amp_living_room'", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
