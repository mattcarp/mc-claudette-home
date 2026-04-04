#!/usr/bin/env python3
"""
Tests for refresh_entities.py — live entity sync from HA .storage files.

Runs against the actual Workshop HA .storage files (storage mode, no token needed).
No mocks. If HA storage files are missing, relevant tests skip gracefully.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
THIS_DIR = Path(__file__).parent
sys.path.insert(0, str(THIS_DIR))

import refresh_entities as re_mod

HA_STORAGE = Path.home() / "homeassistant" / ".storage"
STORAGE_AVAILABLE = (HA_STORAGE / "core.entity_registry").exists()


# ---------------------------------------------------------------------------
# Unit tests (no I/O)
# ---------------------------------------------------------------------------

class TestNormaliseArea(unittest.TestCase):
    def test_sitting_room(self):
        self.assertEqual(re_mod.normalise_area("sitting_room"), "sitting_room")

    def test_living_aliases(self):
        self.assertEqual(re_mod.normalise_area("living"), "living_room")
        self.assertEqual(re_mod.normalise_area("living_room"), "living_room")

    def test_empty_returns_unknown(self):
        self.assertEqual(re_mod.normalise_area(""), "unknown")

    def test_unknown_passthrough(self):
        self.assertEqual(re_mod.normalise_area("rooftop_terrace"), "rooftop_terrace")


class TestShouldExclude(unittest.TestCase):
    def test_hacs_excluded(self):
        self.assertTrue(re_mod.should_exclude("switch.hacs_pre_release"))

    def test_update_excluded(self):
        self.assertTrue(re_mod.should_exclude("update.hacs_update"))

    def test_media_player_not_excluded(self):
        self.assertFalse(re_mod.should_exclude("media_player.xaghra_sitting_room"))

    def test_climate_not_excluded(self):
        self.assertFalse(re_mod.should_exclude("climate.9424b87a6361"))

    def test_sensor_backup_excluded(self):
        self.assertTrue(re_mod.should_exclude("sensor.backup_backup_manager_state"))

    def test_sun_sensor_excluded(self):
        self.assertTrue(re_mod.should_exclude("sensor.sun_next_rising"))


class TestBuildRealEntities(unittest.TestCase):
    def _make_raw(self, domain, entity_id, name="Test", area=""):
        return {
            "entity_id": entity_id,
            "name": name,
            "area": area,
            "area_id": "",
            "domain": domain,
        }

    def test_light_goes_to_lights(self):
        raw = [self._make_raw("light", "light.bedroom", "Bedroom Light", "bedroom")]
        result = re_mod.build_real_entities(raw)
        self.assertEqual(len(result["lights"]), 1)
        self.assertEqual(result["lights"][0]["entity_id"], "light.bedroom")

    def test_media_player_categorised(self):
        raw = [self._make_raw("media_player", "media_player.xaghra_sitting_room", "Sonos", "sitting_room")]
        result = re_mod.build_real_entities(raw)
        self.assertEqual(len(result["media_players"]), 1)
        # Friendly name override should apply
        self.assertEqual(result["media_players"][0]["name"], "Sonos Arc (Sitting Room)")

    def test_climate_categorised(self):
        raw = [self._make_raw("climate", "climate.9424b87a6361", "AC", "sitting_room")]
        result = re_mod.build_real_entities(raw)
        self.assertEqual(len(result["climate"]), 1)

    def test_no_duplicates(self):
        raw = [
            self._make_raw("light", "light.bedroom", "Light"),
            self._make_raw("light", "light.bedroom", "Light"),  # duplicate
        ]
        result = re_mod.build_real_entities(raw)
        self.assertEqual(len(result["lights"]), 1)

    def test_empty_categories_exist(self):
        result = re_mod.build_real_entities([])
        for cat in ["lights", "switches", "media_players", "climate", "covers", "locks", "scenes", "sensors"]:
            self.assertIn(cat, result)

    def test_area_override_applied(self):
        raw = [self._make_raw("media_player", "media_player.living_room_tv", "TV", "")]
        result = re_mod.build_real_entities(raw)
        self.assertEqual(result["media_players"][0]["area"], "living_room")

    def test_sorted_by_area_then_entity_id(self):
        raw = [
            self._make_raw("light", "light.z_light", "Z", "bedroom"),
            self._make_raw("light", "light.a_light", "A", "bedroom"),
        ]
        result = re_mod.build_real_entities(raw)
        ids = [e["entity_id"] for e in result["lights"]]
        self.assertEqual(ids, sorted(ids))


class TestFormatAsPython(unittest.TestCase):
    def test_output_is_valid_python(self):
        entities = {
            "lights": [{"entity_id": "light.test", "name": "Test", "area": "bedroom"}],
            "switches": [],
            "media_players": [],
            "climate": [],
            "covers": [],
            "locks": [],
            "scenes": [],
            "sensors": [],
        }
        output = re_mod.format_as_python(entities, "test")
        # Should be parseable Python
        namespace = {}
        exec(output, namespace)  # noqa: S102
        self.assertIn("REAL_ENTITIES", namespace)
        self.assertEqual(len(namespace["REAL_ENTITIES"]["lights"]), 1)

    def test_empty_categories_omitted(self):
        entities = {k: [] for k in ["lights", "switches", "media_players", "climate", "covers", "locks", "scenes", "sensors"]}
        output = re_mod.format_as_python(entities, "test")
        # With all empty, REAL_ENTITIES block should be minimal
        namespace = {}
        exec(output, namespace)  # noqa: S102
        self.assertIn("REAL_ENTITIES", namespace)


# ---------------------------------------------------------------------------
# Storage integration tests (skipped if HA storage not available)
# ---------------------------------------------------------------------------

@unittest.skipUnless(STORAGE_AVAILABLE, "HA .storage files not available on this host")
class TestStorageMode(unittest.TestCase):
    def setUp(self):
        self.raw = re_mod.fetch_storage_entities()

    def test_returns_list(self):
        self.assertIsInstance(self.raw, list)

    def test_at_least_some_entities(self):
        self.assertGreater(len(self.raw), 0)

    def test_all_have_entity_id(self):
        for e in self.raw:
            self.assertIn("entity_id", e)
            self.assertIn(".", e["entity_id"])

    def test_excluded_entities_removed(self):
        ids = [e["entity_id"] for e in self.raw]
        self.assertNotIn("switch.hacs_pre_release", ids)
        self.assertNotIn("update.hacs_update", ids)
        self.assertNotIn("sensor.backup_backup_manager_state", ids)

    def test_domains_in_domain_map(self):
        for e in self.raw:
            domain = e["entity_id"].split(".")[0]
            self.assertIn(domain, re_mod.DOMAIN_MAP, f"Unexpected domain: {domain} in {e['entity_id']}")

    def test_known_devices_present(self):
        ids = [e["entity_id"] for e in self.raw]
        self.assertIn("media_player.xaghra_sitting_room", ids)
        self.assertIn("climate.9424b87a6361", ids)

    def test_build_full_pipeline(self):
        """End-to-end: fetch → build → format → valid Python."""
        entities_dict = re_mod.build_real_entities(self.raw)
        python_block = re_mod.format_as_python(entities_dict, "test")
        namespace = {}
        exec(python_block, namespace)  # noqa: S102
        real = namespace["REAL_ENTITIES"]
        # Must have media players (Sonos, WiiM)
        mp_ids = [e["entity_id"] for e in real.get("media_players", [])]
        self.assertIn("media_player.xaghra_sitting_room", mp_ids)

    def test_no_unknown_area_for_known_devices(self):
        entities_dict = re_mod.build_real_entities(self.raw)
        for e in entities_dict.get("media_players", []):
            self.assertNotEqual(e.get("area"), "unknown", f"{e['entity_id']} has unknown area")
        for e in entities_dict.get("climate", []):
            self.assertNotEqual(e.get("area"), "unknown", f"{e['entity_id']} has unknown area")

    def test_media_players_have_descriptions(self):
        entities_dict = re_mod.build_real_entities(self.raw)
        for mp in entities_dict.get("media_players", []):
            self.assertIn("description", mp, f"{mp['entity_id']} missing description")


# ---------------------------------------------------------------------------
# ha_context.py sanity check after --write
# ---------------------------------------------------------------------------

class TestHaContextIntegrity(unittest.TestCase):
    """Verify ha_context.py is syntactically valid and REAL_ENTITIES is importable."""

    def test_ha_context_imports_cleanly(self):
        import importlib
        import importlib.util
        ctx_path = THIS_DIR / "ha_context.py"
        spec = importlib.util.spec_from_file_location("ha_context", str(ctx_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, "REAL_ENTITIES"))
        self.assertIsInstance(module.REAL_ENTITIES, dict)

    def test_real_entities_has_expected_keys(self):
        """All structural keys must exist; lights may be empty until Zigbee bulbs are paired."""
        import importlib.util
        ctx_path = THIS_DIR / "ha_context.py"
        spec = importlib.util.spec_from_file_location("ha_context", str(ctx_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real = module.REAL_ENTITIES
        # Structural keys must always be present (may be empty until devices paired)
        for key in ["switches", "media_players", "climate", "sensors"]:
            self.assertIn(key, real, f"Missing key: {key}")
        # Known live devices must be populated
        mp_ids = [e["entity_id"] for e in real.get("media_players", [])]
        self.assertIn("media_player.xaghra_sitting_room", mp_ids, "Sonos Arc missing from REAL_ENTITIES")

    def test_build_entity_summary_works(self):
        import importlib.util
        ctx_path = THIS_DIR / "ha_context.py"
        spec = importlib.util.spec_from_file_location("ha_context", str(ctx_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        summary = module.build_entity_summary(module.REAL_ENTITIES)
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 50)
        self.assertIn("media_player", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
