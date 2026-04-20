#!/usr/bin/env python3
"""
Claudette Home — Intent Parser Test Suite
Validates that natural language utterances map to the expected HA actions.

Tests run WITHOUT calling the API — they validate the context builder,
JSON structure, and the parser's output contract.

For live API tests (requires an API key):
  python3 test_intent_parser.py --live
  python3 test_intent_parser.py --live --backend openrouter
  python3 test_intent_parser.py --live --backend openai

For dry-run structural tests:
  python3 test_intent_parser.py
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add parent dir to path for import
sys.path.insert(0, os.path.dirname(__file__))

from ha_context import build_entity_summary, build_system_prompt, SAMPLE_ENTITIES
from intent_parser import parse_intent, format_action_summary


class TestHAContextBuilder(unittest.TestCase):
    """Test the context builder — no API calls."""

    def test_entity_summary_contains_lights(self):
        summary = build_entity_summary()
        self.assertIn("light.living_room", summary)
        self.assertIn("Living Room Light", summary)

    def test_entity_summary_contains_scenes(self):
        summary = build_entity_summary()
        self.assertIn("scene.goodnight", summary)
        self.assertIn("scene.dinner", summary)
        self.assertIn("scene.leaving", summary)

    def test_entity_summary_contains_locks(self):
        summary = build_entity_summary()
        self.assertIn("lock.front_door", summary)

    def test_system_prompt_has_rules(self):
        prompt = build_system_prompt()
        self.assertIn("JSON", prompt)
        self.assertIn("call_service", prompt)
        self.assertIn("clarify", prompt)

    def test_system_prompt_mentions_audio_transport_services(self):
        prompt = build_system_prompt()
        self.assertIn("media_next_track", prompt)
        self.assertIn("volume_up", prompt)
        self.assertIn("volume_set", prompt)

    def test_system_prompt_includes_entity_summary(self):
        prompt = build_system_prompt()
        self.assertIn("light.living_room", prompt)
        self.assertIn("scene.goodnight", prompt)

    def test_custom_entities(self):
        custom = {
            "lights": [
                {"entity_id": "light.test_room", "name": "Test Room", "area": "test"},
            ]
        }
        summary = build_entity_summary(custom)
        self.assertIn("light.test_room", summary)
        self.assertNotIn("light.living_room", summary)


class TestIntentParserMocked(unittest.TestCase):
    """Test parse_intent() with mocked API responses."""

    def _make_mock_client(self, json_response: str):
        """Build a mock Anthropic client that returns the given JSON string."""
        mock_content = MagicMock()
        mock_content.text = json_response

        mock_message = MagicMock()
        mock_message.content = [mock_content]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        return mock_client

    def test_lights_off(self):
        resp = json.dumps({
            "action": "call_service",
            "domain": "light",
            "service": "turn_off",
            "entity_id": "light.all",
            "params": {}
        })
        client = self._make_mock_client(resp)
        result = parse_intent("turn off the lights", client=client)
        self.assertEqual(result["action"], "call_service")
        self.assertEqual(result["domain"], "light")
        self.assertEqual(result["service"], "turn_off")

    def test_scene_goodnight(self):
        resp = json.dumps({
            "action": "call_service",
            "domain": "scene",
            "service": "activate",
            "entity_id": "scene.goodnight",
            "params": {}
        })
        client = self._make_mock_client(resp)
        result = parse_intent("I'm going to bed", client=client)
        self.assertEqual(result["entity_id"], "scene.goodnight")

    def test_dim_with_brightness(self):
        resp = json.dumps({
            "action": "call_service",
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.living_room",
            "params": {"brightness_pct": 40}
        })
        client = self._make_mock_client(resp)
        result = parse_intent("dim the living room to 40%", client=client)
        self.assertEqual(result["params"]["brightness_pct"], 40)

    def test_multi_action(self):
        resp = json.dumps([
            {"action": "call_service", "domain": "scene", "service": "activate",
             "entity_id": "scene.dinner", "params": {}},
            {"action": "call_service", "domain": "lock", "service": "lock",
             "entity_id": "lock.front_door", "params": {}}
        ])
        client = self._make_mock_client(resp)
        result = parse_intent("set up for dinner and lock the front door", client=client)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["entity_id"], "scene.dinner")
        self.assertEqual(result[1]["entity_id"], "lock.front_door")

    def test_clarify(self):
        resp = json.dumps({
            "action": "clarify",
            "question": "Which room did you mean?"
        })
        client = self._make_mock_client(resp)
        result = parse_intent("turn on the thing", client=client)
        self.assertEqual(result["action"], "clarify")
        self.assertIn("question", result)

    def test_query(self):
        resp = json.dumps({
            "action": "query",
            "entity_id": "sensor.living_room_temperature",
            "question": "What is the current temperature?"
        })
        client = self._make_mock_client(resp)
        result = parse_intent("what's the temperature?", client=client)
        self.assertEqual(result["action"], "query")

    def test_media_next_track_intent_shape(self):
        resp = json.dumps({
            "action": "call_service",
            "domain": "media_player",
            "service": "media_next_track",
            "entity_id": "media_player.xaghra_sitting_room",
            "params": {}
        })
        client = self._make_mock_client(resp)
        result = parse_intent("skip this track", client=client)
        self.assertEqual(result["domain"], "media_player")
        self.assertEqual(result["service"], "media_next_track")

    def test_media_volume_up_intent_shape(self):
        resp = json.dumps({
            "action": "call_service",
            "domain": "media_player",
            "service": "volume_up",
            "entity_id": "media_player.bedroom_speaker",
            "params": {}
        })
        client = self._make_mock_client(resp)
        result = parse_intent("turn it up in the bedroom", client=client)
        self.assertEqual(result["domain"], "media_player")
        self.assertEqual(result["service"], "volume_up")

    def test_media_volume_set_intent_shape(self):
        resp = json.dumps({
            "action": "call_service",
            "domain": "media_player",
            "service": "volume_set",
            "entity_id": "media_player.living_room_speaker",
            "params": {"volume_level": 0.4}
        })
        client = self._make_mock_client(resp)
        result = parse_intent("set the living room speaker to 40 percent", client=client)
        self.assertEqual(result["domain"], "media_player")
        self.assertEqual(result["service"], "volume_set")
        self.assertEqual(result["params"]["volume_level"], 0.4)

    def test_handles_markdown_fences(self):
        """Model wraps JSON in code fences — we strip them."""
        resp = "```json\n{\"action\": \"clarify\", \"question\": \"Which room?\"}\n```"
        client = self._make_mock_client(resp)
        result = parse_intent("turn on the thing", client=client)
        self.assertEqual(result["action"], "clarify")

    def test_invalid_json_raises(self):
        """Non-JSON response raises ValueError."""
        client = self._make_mock_client("Sorry, I don't understand that.")
        with self.assertRaises(ValueError):
            parse_intent("do something weird", client=client)


class TestFormatActionSummary(unittest.TestCase):
    """Test human-readable action formatting."""

    def test_single_action(self):
        action = {
            "action": "call_service",
            "domain": "light",
            "service": "turn_off",
            "entity_id": "light.living_room",
            "params": {}
        }
        summary = format_action_summary(action)
        self.assertIn("light.turn_off", summary)
        self.assertIn("light.living_room", summary)

    def test_multi_action(self):
        actions = [
            {"action": "call_service", "domain": "scene", "service": "activate",
             "entity_id": "scene.dinner", "params": {}},
            {"action": "call_service", "domain": "lock", "service": "lock",
             "entity_id": "lock.front_door", "params": {}}
        ]
        summary = format_action_summary(actions)
        self.assertIn(" + ", summary)
        self.assertIn("scene.activate", summary)

    def test_clarify(self):
        action = {"action": "clarify", "question": "Which room?"}
        summary = format_action_summary(action)
        self.assertIn("CLARIFY", summary)
        self.assertIn("Which room?", summary)


class TestMultiBackend(unittest.TestCase):
    """Tests for multi-backend support and backend auto-detection."""

    def _make_openai_mock(self, json_response: str):
        """Build a mock that matches openai.OpenAI().chat.completions.create()."""
        mock_message = MagicMock()
        mock_message.content = json_response

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_completions = MagicMock()
        mock_completions.create.return_value = mock_response

        mock_chat = MagicMock()
        mock_chat.completions = mock_completions

        mock_client = MagicMock()
        mock_client.chat = mock_chat

        return mock_client

    def test_detect_backend_openrouter_first(self):
        """OpenRouter is preferred when both keys exist."""
        from intent_parser import _detect_backend
        orig_or = os.environ.get("OPENROUTER_API_KEY")
        orig_oai = os.environ.get("OPENAI_API_KEY")
        orig_ant = os.environ.get("ANTHROPIC_API_KEY")
        orig_explicit = os.environ.get("HA_INTENT_BACKEND")
        try:
            os.environ["OPENROUTER_API_KEY"] = "test-key"
            os.environ["OPENAI_API_KEY"] = "test-key"
            os.environ["ANTHROPIC_API_KEY"] = "test-key"
            os.environ.pop("HA_INTENT_BACKEND", None)
            backend = _detect_backend()
            self.assertEqual(backend, "openrouter")
        finally:
            for k, v in [
                ("OPENROUTER_API_KEY", orig_or),
                ("OPENAI_API_KEY", orig_oai),
                ("ANTHROPIC_API_KEY", orig_ant),
                ("HA_INTENT_BACKEND", orig_explicit),
            ]:
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_detect_backend_explicit_override(self):
        """HA_INTENT_BACKEND env var overrides auto-detection."""
        from intent_parser import _detect_backend
        orig = os.environ.get("HA_INTENT_BACKEND")
        try:
            os.environ["HA_INTENT_BACKEND"] = "anthropic"
            self.assertEqual(_detect_backend(), "anthropic")
            os.environ["HA_INTENT_BACKEND"] = "openai"
            self.assertEqual(_detect_backend(), "openai")
        finally:
            if orig is not None:
                os.environ["HA_INTENT_BACKEND"] = orig
            else:
                os.environ.pop("HA_INTENT_BACKEND", None)

    def test_detect_backend_no_keys_raises(self):
        """Raises EnvironmentError when no keys are set."""
        from intent_parser import _detect_backend
        saved = {}
        for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HA_INTENT_BACKEND"):
            saved[k] = os.environ.pop(k, None)
        try:
            with self.assertRaises(EnvironmentError):
                _detect_backend()
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_openrouter_backend_via_mock(self):
        """OpenRouter backend parses intent correctly when mocked."""
        resp = json.dumps({
            "action": "call_service",
            "domain": "scene",
            "service": "activate",
            "entity_id": "scene.goodnight",
            "params": {}
        })

        with patch("intent_parser._call_openrouter", return_value=resp):
            result = parse_intent(
                "I'm going to bed",
                backend="openrouter",
            )
        self.assertEqual(result["entity_id"], "scene.goodnight")

    def test_openai_backend_via_mock(self):
        """OpenAI backend parses intent correctly when mocked."""
        resp = json.dumps({
            "action": "call_service",
            "domain": "light",
            "service": "turn_off",
            "entity_id": "light.all",
            "params": {}
        })

        with patch("intent_parser._call_openai", return_value=resp):
            result = parse_intent(
                "turn off the lights",
                backend="openai",
            )
        self.assertEqual(result["action"], "call_service")
        self.assertEqual(result["service"], "turn_off")

    def test_strip_fences(self):
        """_strip_fences handles markdown code blocks correctly."""
        from intent_parser import _strip_fences
        raw = '```json\n{"action": "clarify", "question": "Which room?"}\n```'
        stripped = _strip_fences(raw)
        self.assertFalse(stripped.startswith("```"))
        result = json.loads(stripped)
        self.assertEqual(result["action"], "clarify")

    def test_strip_fences_no_fence_unchanged(self):
        """_strip_fences doesn't corrupt plain JSON."""
        from intent_parser import _strip_fences
        raw = '{"action": "clarify", "question": "Which room?"}'
        self.assertEqual(_strip_fences(raw), raw)


class TestMultilingualPrompt(unittest.TestCase):
    """Test that the system prompt contains multilingual instructions."""

    def test_system_prompt_mentions_maltese(self):
        prompt = build_system_prompt()
        self.assertIn("Maltese", prompt)
        self.assertIn("Malti", prompt)

    def test_system_prompt_mentions_italian(self):
        prompt = build_system_prompt()
        self.assertIn("Italian", prompt)

    def test_system_prompt_mentions_arabic(self):
        prompt = build_system_prompt()
        self.assertIn("Arabic", prompt)

    def test_system_prompt_has_maltese_examples(self):
        prompt = build_system_prompt()
        # Check for at least one Maltese phrase
        self.assertTrue(
            "Agħlaq" in prompt or "Iftaħ" in prompt or "Sejjer" in prompt,
            "Expected at least one Maltese phrase in system prompt"
        )


class TestLiveAPI(unittest.TestCase):
    """
    Live API tests — only run with --live flag and an API key set.
    These actually call a real model API and validate responses.

    Backends tested in priority order: openrouter → openai → anthropic.
    """
    LIVE_CASES = [
        ("turn off the lights", "call_service"),
        ("I'm going to bed", "call_service"),
        ("what's the temperature?", "query"),
        ("dim the living room to 30%", "call_service"),
        ("set up for dinner", "call_service"),
        ("I'm heading out", "call_service"),
        ("lock the front door", "call_service"),
    ]

    # Multilingual cases — parsed by the model regardless of input language
    MULTILINGUAL_CASES = [
        ("Agħlaq id-dawl", "call_service"),       # Maltese: turn off the lights
        ("Sejjer norqod", "call_service"),          # Maltese: I'm going to bed
        ("Spegni le luci", "call_service"),         # Italian: turn off the lights
    ]

    @classmethod
    def setUpClass(cls):
        from intent_parser import _detect_backend
        try:
            cls.backend = _detect_backend()
        except EnvironmentError:
            raise unittest.SkipTest("No API key found — skipping live tests")

    def test_live_utterances(self):
        for transcript, expected_action in self.LIVE_CASES:
            with self.subTest(transcript=transcript):
                result = parse_intent(transcript, backend=self.backend)
                if isinstance(result, list):
                    actual_actions = [a.get("action") for a in result]
                    self.assertTrue(
                        all(a == expected_action for a in actual_actions),
                        f"Expected all {expected_action!r} for: {transcript!r}\nGot: {result}"
                    )
                else:
                    self.assertEqual(
                        result.get("action"), expected_action,
                        f"Expected {expected_action!r} for: {transcript!r}\nGot: {result}"
                    )
                print(f"  ✓ [{self.backend}] {transcript!r} → {format_action_summary(result)}")

    def test_multilingual_utterances(self):
        """Multilingual home commands should parse to valid HA actions."""
        for transcript, expected_action in self.MULTILINGUAL_CASES:
            with self.subTest(transcript=transcript):
                result = parse_intent(transcript, backend=self.backend)
                if isinstance(result, list):
                    actual_actions = [a.get("action") for a in result]
                    self.assertTrue(
                        any(a == expected_action for a in actual_actions),
                        f"Expected {expected_action!r} for: {transcript!r}\nGot: {result}"
                    )
                else:
                    self.assertEqual(
                        result.get("action"), expected_action,
                        f"Expected {expected_action!r} for: {transcript!r}\nGot: {result}"
                    )
                print(f"  ✓ [{self.backend}] {transcript!r} → {format_action_summary(result)}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Run live API tests")
    ap.add_argument("--backend", default=None,
                    choices=["anthropic", "openai", "openrouter"],
                    help="Backend to use for live tests (default: auto-detect)")
    args, remaining = ap.parse_known_args()

    if args.backend:
        os.environ["HA_INTENT_BACKEND"] = args.backend

    if args.live:
        # Include live test class
        suite = unittest.TestLoader().loadTestsFromTestCase(TestLiveAPI)
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestHAContextBuilder))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestIntentParserMocked))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestMultiBackend))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestMultilingualPrompt))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestFormatActionSummary))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    else:
        # Dry run — no live API
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        suite.addTests(loader.loadTestsFromTestCase(TestHAContextBuilder))
        suite.addTests(loader.loadTestsFromTestCase(TestIntentParserMocked))
        suite.addTests(loader.loadTestsFromTestCase(TestMultiBackend))
        suite.addTests(loader.loadTestsFromTestCase(TestMultilingualPrompt))
        suite.addTests(loader.loadTestsFromTestCase(TestFormatActionSummary))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
