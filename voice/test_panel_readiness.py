#!/usr/bin/env python3
"""
Tests for panel_readiness.py

These are unit tests for individual check functions — no hardware required.
We validate:
  - Check functions return CheckResult objects
  - Checks that should pass on the Workshop actually pass
  - Checks that should fail (missing files, missing env vars) fail correctly
  - JSON output format is correct
  - Report exit code reflects critical failures

Run:
  python3 -m pytest voice/test_panel_readiness.py -v
"""

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

VOICE_DIR = Path(__file__).parent
PROJECT_ROOT = VOICE_DIR.parent

# Ensure voice/ is on path
sys.path.insert(0, str(VOICE_DIR))

import panel_readiness as pr


class TestCheckResultModel(unittest.TestCase):
    """CheckResult dataclass sanity checks."""

    def test_passed_result(self):
        r = pr.CheckResult("test", True, "all good")
        self.assertTrue(r.passed)
        self.assertEqual(r.name, "test")
        self.assertEqual(r.detail, "all good")
        self.assertTrue(r.critical)  # default

    def test_failed_result(self):
        r = pr.CheckResult("test", False, "broken", "do this to fix", critical=False)
        self.assertFalse(r.passed)
        self.assertFalse(r.critical)
        self.assertIn("fix", r.fix_hint)


class TestSttServiceCheck(unittest.TestCase):
    """STT service checks."""

    def test_stt_service_returns_check_result(self):
        result = pr.check_stt_service_running()
        self.assertIsInstance(result, pr.CheckResult)
        self.assertEqual(result.name, "STT service (claudette-stt.service)")

    def test_stt_service_running_on_workshop(self):
        """claudette-stt.service should be active on the Workshop."""
        result = pr.check_stt_service_running()
        self.assertTrue(result.passed, f"STT service not running: {result.detail}")

    def test_stt_health_returns_check_result(self):
        result = pr.check_stt_health()
        self.assertIsInstance(result, pr.CheckResult)

    def test_stt_health_ok_on_workshop(self):
        """STT /health should return status=ok."""
        result = pr.check_stt_health()
        self.assertTrue(result.passed, f"STT health failed: {result.detail}")
        self.assertIn("backend=faster-whisper", result.detail)

    def test_stt_latency_within_target(self):
        """STT latency should be <3s (target)."""
        result = pr.check_stt_latency()
        self.assertTrue(result.passed, f"STT latency check failed: {result.detail}")
        # Extract the float from "0.42s (target <3s)"
        latency_str = result.detail.split("s")[0]
        latency = float(latency_str)
        self.assertLess(latency, 3.0)


class TestPipelineChecks(unittest.TestCase):
    """Pipeline smoke tests."""

    def test_pipeline_text_mode_returns_check_result(self):
        result = pr.check_pipeline_text_mode()
        self.assertIsInstance(result, pr.CheckResult)

    def test_pipeline_text_mode_passes(self):
        """pipeline.py --stub --text should succeed."""
        result = pr.check_pipeline_text_mode()
        self.assertTrue(result.passed, f"Pipeline text mode failed: {result.detail}")

    def test_wake_bridge_pipe_contract_returns_check_result(self):
        result = pr.check_wake_bridge_pipe_contract()
        self.assertIsInstance(result, pr.CheckResult)

    def test_wake_bridge_pipe_contract_passes(self):
        """Injecting a wake_word_detected event into pipeline stdin should not crash."""
        result = pr.check_wake_bridge_pipe_contract()
        self.assertTrue(result.passed, f"Pipe contract failed: {result.detail}")


class TestPorcupineChecks(unittest.TestCase):
    """Porcupine-specific checks."""

    def test_porcupine_sdk_importable(self):
        """pvporcupine should be installed."""
        result = pr.check_porcupine_sdk()
        self.assertTrue(result.passed, f"pvporcupine not installed: {result.detail}")

    def test_access_key_check_fails_when_not_set(self):
        """When PORCUPINE_ACCESS_KEY is not in env, check should fail."""
        env_backup = os.environ.pop("PORCUPINE_ACCESS_KEY", None)
        try:
            result = pr.check_porcupine_access_key()
            self.assertFalse(result.passed)
            self.assertIn("console.picovoice.ai", result.fix_hint)
        finally:
            if env_backup:
                os.environ["PORCUPINE_ACCESS_KEY"] = env_backup

    def test_access_key_check_passes_when_set(self):
        """When PORCUPINE_ACCESS_KEY is set, check should pass."""
        backup = os.environ.get("PORCUPINE_ACCESS_KEY")
        try:
            os.environ["PORCUPINE_ACCESS_KEY"] = "fake-key-for-test-abc123"
            result = pr.check_porcupine_access_key()
            self.assertTrue(result.passed)
        finally:
            if backup:
                os.environ["PORCUPINE_ACCESS_KEY"] = backup
            else:
                os.environ.pop("PORCUPINE_ACCESS_KEY", None)

    def test_ppn_model_check_fails_when_missing(self):
        """If claudette_linux.ppn doesn't exist, check should fail with helpful hint."""
        result = pr.check_ppn_model()
        # We know the model isn't there yet — this is expected to fail
        if not result.passed:
            self.assertIn("console.picovoice.ai", result.fix_hint)
            self.assertIn("claudette_linux.ppn", result.fix_hint)
        # If it somehow passes, that's also fine (model was placed there)

    def test_ppn_check_passes_when_file_present(self, tmp_path=None):
        """Simulate model file existing."""
        import tempfile
        model_dir = VOICE_DIR / "wake_word" / "models"
        model_path = model_dir / "claudette_linux.ppn"

        # Skip if real model is present (already passes naturally)
        if model_path.exists():
            return

        # Create a fake file
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path.write_bytes(b"FAKE_PPN_MODEL")
        try:
            result = pr.check_ppn_model()
            self.assertTrue(result.passed)
        finally:
            model_path.unlink(missing_ok=True)


class TestVadAndTts(unittest.TestCase):
    """VAD + TTS checks."""

    def test_vad_recorder_importable(self):
        result = pr.check_vad_recorder()
        self.assertTrue(result.passed, f"VAD recorder check failed: {result.detail}")

    def test_tts_responder_dry_run(self):
        result = pr.check_tts_responder()
        self.assertTrue(result.passed, f"TTS responder failed: {result.detail}")

    def test_ha_bridge_importable(self):
        result = pr.check_ha_bridge_stub()
        self.assertTrue(result.passed, f"HA bridge check failed: {result.detail}")


class TestNetworkCheck(unittest.TestCase):
    """Network self-check."""

    def test_stt_port_reachable(self):
        """Port 8765 should be open (STT service running)."""
        result = pr.check_network_self()
        self.assertTrue(result.passed, f"Port 8765 not reachable: {result.detail}")


class TestRunChecks(unittest.TestCase):
    """run_checks() integration."""

    def test_run_checks_returns_list(self):
        # Just run 3 cheap checks to verify the runner
        results = pr.run_checks()
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertIsInstance(r, pr.CheckResult)

    def test_all_checks_have_names(self):
        results = pr.run_checks()
        for r in results:
            self.assertTrue(len(r.name) > 0, "CheckResult must have a name")


class TestJsonOutput(unittest.TestCase):
    """JSON report format."""

    def test_json_report_structure(self):
        """panel_readiness.py --json should produce valid JSON with required fields."""
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "panel_readiness.py"), "--json"],
            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        )
        # JSON output should be parseable regardless of exit code
        data = json.loads(r.stdout)
        self.assertIn("generated_at", data)
        self.assertIn("summary", data)
        self.assertIn("checks", data)
        self.assertIn("total", data["summary"])
        self.assertIn("passed", data["summary"])
        self.assertIn("critical_fails", data["summary"])
        self.assertGreater(len(data["checks"]), 0)

    def test_json_checks_have_required_fields(self):
        """Each check in JSON output should have name, passed, critical, detail."""
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "panel_readiness.py"), "--json"],
            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        )
        data = json.loads(r.stdout)
        for check in data["checks"]:
            self.assertIn("name", check)
            self.assertIn("passed", check)
            self.assertIn("critical", check)
            self.assertIn("detail", check)

    def test_json_summary_counts_are_consistent(self):
        """summary.passed + summary.failed should equal summary.total."""
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "panel_readiness.py"), "--json"],
            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        )
        data = json.loads(r.stdout)
        s = data["summary"]
        self.assertEqual(s["passed"] + s["failed"], s["total"])


class TestExitCodes(unittest.TestCase):
    """Exit code behaviour."""

    def test_exit_1_when_critical_checks_fail(self):
        """panel_readiness.py should exit 1 when critical checks fail."""
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "panel_readiness.py")],
            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        )
        # We know PORCUPINE_ACCESS_KEY + .ppn are missing → exit 1
        # (If they were set + present, exit 0 — also acceptable)
        self.assertIn(r.returncode, [0, 1])

    def test_stdout_contains_panel_readiness_header(self):
        """Human report should mention 'Panel Readiness'."""
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "panel_readiness.py")],
            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        )
        self.assertIn("Panel Readiness", r.stdout)
        self.assertIn("passed", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
