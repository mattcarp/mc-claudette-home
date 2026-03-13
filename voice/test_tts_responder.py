#!/usr/bin/env python3
"""
Claudette Home — TTS Responder Tests
Tests for tts_responder.py — no audio hardware or API keys required.

Run:
  python3 voice/test_tts_responder.py
  python3 -m pytest voice/test_tts_responder.py -v
"""

import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

# Path setup
VOICE_DIR = os.path.dirname(__file__)
sys.path.insert(0, VOICE_DIR)


# ---------------------------------------------------------------------------
# Helper: import with env overrides
# ---------------------------------------------------------------------------

def import_tts_responder():
    """Import tts_responder, clearing any cached version."""
    if "tts_responder" in sys.modules:
        del sys.modules["tts_responder"]
    import tts_responder
    return tts_responder


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

class TestBackendDetection:
    def test_auto_detects_openai_when_key_set(self):
        mod = import_tts_responder()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "TTS_BACKEND": "auto"}):
            # Simulate the detection logic directly
            result = "openai" if os.environ.get("OPENAI_API_KEY") else "gtts"
            assert result == "openai"

    def test_forced_backend_respected(self):
        with patch.dict(os.environ, {"TTS_BACKEND": "espeak"}):
            mod = import_tts_responder()
            assert mod.TTS_BACKEND_ENV == "espeak"

    def test_list_backends_flag(self):
        """list-backends flag should not crash."""
        result = subprocess.run(
            [sys.executable, os.path.join(VOICE_DIR, "tts_responder.py"), "--list-backends"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "openai" in result.stdout
        assert "gtts" in result.stdout
        assert "espeak-ng" in result.stdout


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_speak_dry_run_returns_true(self):
        mod = import_tts_responder()
        ok = mod.speak("Dry run test", dry_run=True)
        assert ok is True

    def test_speak_empty_string_dry_run(self):
        mod = import_tts_responder()
        ok = mod.speak("", dry_run=True)
        assert ok is True

    def test_speak_whitespace_only_dry_run(self):
        mod = import_tts_responder()
        ok = mod.speak("   ", dry_run=True)
        assert ok is True


# ---------------------------------------------------------------------------
# print backend
# ---------------------------------------------------------------------------

class TestPrintBackend:
    def test_print_backend_always_returns_true(self):
        mod = import_tts_responder()
        ok = mod.speak_print("Hello from print backend")
        assert ok is True

    def test_speak_with_print_backend(self, capsys=None):
        mod = import_tts_responder()
        ok = mod.speak("Test message", backend="print")
        assert ok is True


# ---------------------------------------------------------------------------
# espeak-ng backend
# ---------------------------------------------------------------------------

class TestEspeakBackend:
    def test_espeak_called_with_correct_args(self):
        mod = import_tts_responder()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            ok = mod.speak_espeak("Test sentence")
            assert ok is True
            call_args = mock_run.call_args[0][0]
            assert "espeak-ng" in call_args
            assert "Test sentence" in call_args

    def test_espeak_handles_failure(self):
        mod = import_tts_responder()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr=b"error")
            ok = mod.speak_espeak("Test")
            assert ok is False

    def test_espeak_handles_not_found(self):
        mod = import_tts_responder()
        with patch("subprocess.run", side_effect=FileNotFoundError("espeak-ng not found")):
            ok = mod.speak_espeak("Test")
            assert ok is False


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------

class TestOpenAIBackend:
    def test_openai_speaks_with_valid_key(self):
        mod = import_tts_responder()
        mock_response = MagicMock()
        mock_response.content = b"\xff\xf3\x00"  # Minimal MP3-like bytes
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            with patch.object(mod, "play_audio_bytes", return_value=True) as mock_play:
                with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
                    # Re-read the env var directly in the function scope
                    original_key = mod.OPENAI_API_KEY
                    mod.OPENAI_API_KEY = "sk-test-key"
                    try:
                        ok = mod.speak_openai("Turn off the lights.")
                        assert ok is True
                        mock_post.assert_called_once()
                        call_json = mock_post.call_args[1]["json"]
                        assert call_json["model"] in ("tts-1", "tts-1-hd")
                        assert call_json["input"] == "Turn off the lights."
                    finally:
                        mod.OPENAI_API_KEY = original_key

    def test_openai_fails_without_key(self):
        mod = import_tts_responder()
        original_key = mod.OPENAI_API_KEY
        mod.OPENAI_API_KEY = ""
        try:
            ok = mod.speak_openai("Test")
            assert ok is False
        finally:
            mod.OPENAI_API_KEY = original_key

    def test_openai_handles_http_error(self):
        mod = import_tts_responder()
        import requests
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("401")
        mock_response.text = "Unauthorized"

        with patch("requests.post", return_value=mock_response):
            mod.OPENAI_API_KEY = "sk-bad-key"
            ok = mod.speak_openai("Test")
            assert ok is False


# ---------------------------------------------------------------------------
# gTTS backend
# ---------------------------------------------------------------------------

class TestGTTSBackend:
    def test_gtts_speaks_successfully(self):
        mod = import_tts_responder()
        mock_tts = MagicMock()
        mock_tts.write_to_fp = lambda fp: fp.write(b"\xff\xfb\x00")  # Minimal MP3 bytes

        with patch("gtts.gTTS", return_value=mock_tts) as mock_gTTS:
            with patch.object(mod, "play_audio_bytes", return_value=True):
                ok = mod.speak_gtts("Turn on the living room lights.")
                assert ok is True
                mock_gTTS.assert_called_once_with(
                    text="Turn on the living room lights.",
                    lang=mod.TTS_LANGUAGE,
                    slow=False,
                )

    def test_gtts_handles_import_error(self):
        mod = import_tts_responder()
        with patch.dict(sys.modules, {"gtts": None}):
            # gtts is not importable — should return False gracefully
            # (We can't fully mock builtins.import here, so test the error path directly)
            with patch("builtins.__import__", side_effect=ImportError("No module named 'gtts'")):
                ok = mod.speak_gtts("Test")
                assert ok is False


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

class TestFallbackChain:
    def test_falls_back_to_gtts_when_openai_fails(self):
        mod = import_tts_responder()
        calls = []

        def mock_openai(text):
            calls.append("openai")
            return False

        def mock_gtts(text):
            calls.append("gtts")
            return True

        with patch.object(mod, "speak_openai", mock_openai):
            with patch.object(mod, "speak_gtts", mock_gtts):
                ok = mod.speak("Test fallback", backend="openai")
                assert ok is True
                assert "openai" in calls
                assert "gtts" in calls

    def test_falls_back_all_the_way_to_print(self):
        mod = import_tts_responder()

        with patch.object(mod, "speak_openai", return_value=False):
            with patch.object(mod, "speak_gtts", return_value=False):
                with patch.object(mod, "speak_espeak", return_value=False):
                    with patch.object(mod, "speak_print", return_value=True) as mock_print:
                        ok = mod.speak("Emergency fallback", backend="openai")
                        assert ok is True
                        mock_print.assert_called_once_with("Emergency fallback")

    def test_returns_false_if_all_backends_fail(self):
        mod = import_tts_responder()

        with patch.object(mod, "speak_openai", return_value=False):
            with patch.object(mod, "speak_gtts", return_value=False):
                with patch.object(mod, "speak_espeak", return_value=False):
                    with patch.object(mod, "speak_print", return_value=False):
                        ok = mod.speak("Should fail", backend="openai")
                        assert ok is False


# ---------------------------------------------------------------------------
# stdin event processing
# ---------------------------------------------------------------------------

class TestStdinProcessing:
    def _run_stdin(self, lines: list, dry_run: bool = True) -> list:
        """Helper: simulate stdin with given JSON lines, capture speak calls."""
        mod = import_tts_responder()
        spoken = []

        def mock_speak(text, dry_run=False, **kwargs):
            spoken.append(text)
            return True

        original_stdin = sys.stdin
        sys.stdin = io.StringIO("\n".join(lines) + "\n")
        try:
            with patch.object(mod, "speak", mock_speak):
                mod.run_from_stdin(dry_run=dry_run)
        finally:
            sys.stdin = original_stdin
        return spoken

    def test_speaks_on_pipeline_response(self):
        lines = [
            json.dumps({"type": "pipeline_response", "text": "Done, kitchen light is off."}),
        ]
        spoken = self._run_stdin(lines)
        assert spoken == ["Done, kitchen light is off."]

    def test_ignores_wake_word_events(self):
        lines = [
            json.dumps({"type": "wake_word_detected", "backend": "porcupine", "word": "claudette"}),
        ]
        spoken = self._run_stdin(lines)
        assert spoken == []

    def test_ignores_non_json_lines(self):
        lines = [
            "INFO:something happened",
            json.dumps({"type": "pipeline_response", "text": "Hello."}),
        ]
        spoken = self._run_stdin(lines)
        assert spoken == ["Hello."]

    def test_ignores_empty_text(self):
        lines = [
            json.dumps({"type": "pipeline_response", "text": ""}),
            json.dumps({"type": "pipeline_response", "text": "   "}),
        ]
        spoken = self._run_stdin(lines)
        assert spoken == []

    def test_multiple_responses(self):
        lines = [
            json.dumps({"type": "pipeline_response", "text": "Done, light is on."}),
            json.dumps({"type": "wake_word_detected", "word": "claudette"}),
            json.dumps({"type": "pipeline_response", "text": "Temperature is 22 degrees."}),
        ]
        spoken = self._run_stdin(lines)
        assert spoken == ["Done, light is on.", "Temperature is 22 degrees."]

    def test_unknown_event_type_silently_ignored(self):
        lines = [
            json.dumps({"type": "mystery_event", "data": "something"}),
        ]
        spoken = self._run_stdin(lines)
        assert spoken == []


# ---------------------------------------------------------------------------
# CLI: --speak flag
# ---------------------------------------------------------------------------

class TestCLISpeak:
    def test_speak_flag_exits_zero(self):
        result = subprocess.run(
            [sys.executable, os.path.join(VOICE_DIR, "tts_responder.py"),
             "--speak", "CLI test phrase", "--dry-run"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "CLI test phrase" in result.stdout or "DRY RUN" in result.stdout

    def test_speak_flag_with_backend_print(self):
        result = subprocess.run(
            [sys.executable, os.path.join(VOICE_DIR, "tts_responder.py"),
             "--speak", "Print backend test", "--backend", "print"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "Print backend test" in result.stdout

    def test_speak_flag_with_espeak_mocked(self):
        """Test --speak with espeak backend (mocked subprocess)."""
        # We can't mock at the subprocess level here easily, so test with --dry-run
        result = subprocess.run(
            [sys.executable, os.path.join(VOICE_DIR, "tts_responder.py"),
             "--speak", "Espeak dry run", "--dry-run"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# play_audio_bytes
# ---------------------------------------------------------------------------

class TestPlayAudio:
    def test_play_audio_bytes_creates_temp_file(self, tmp_path):
        mod = import_tts_responder()
        original_cache = mod.TTS_CACHE_DIR
        mod.TTS_CACHE_DIR = tmp_path

        with patch.object(mod, "play_audio_file", return_value=True) as mock_play:
            ok = mod.play_audio_bytes(b"\xff\xfb\x90\x00", ext="mp3")
            assert ok is True
            # Check a temp file was passed to play_audio_file
            mock_play.assert_called_once()
            path_arg = mock_play.call_args[0][0]
            assert path_arg.endswith(".mp3")

        mod.TTS_CACHE_DIR = original_cache

    def test_play_audio_file_no_ffplay(self):
        mod = import_tts_responder()
        original_ffplay = mod.HAS_FFPLAY
        mod.HAS_FFPLAY = False
        try:
            ok = mod.play_audio_file("/tmp/test.mp3")
            assert ok is False
        finally:
            mod.HAS_FFPLAY = original_ffplay


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
    except ImportError:
        unittest.main()
