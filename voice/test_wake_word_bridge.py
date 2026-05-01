#!/usr/bin/env python3
"""
Tests for voice/wake_word/wake_word_bridge.py

No audio hardware required.
Focuses on deterministic stub mode and event contract.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "voice" / "wake_word" / "wake_word_bridge.py"


def _run(*args: str, timeout: int = 10):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(ROOT),
    )


def _json_lines(stdout: str):
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        events.append(json.loads(line))
    return events


def test_stub_backend_emits_listener_lifecycle_and_wake_events():
    result = _run("--backend", "stub", "--max-events", "3", "--interval", "0")
    assert result.returncode == 0, result.stderr

    events = _json_lines(result.stdout)
    assert len(events) == 8  # start + (wake+stt_skipped)*3 + stop

    assert events[0]["type"] == "listener_started"
    assert events[0]["backend"] == "stub"

    wake_events = [e for e in events if e["type"] == "wake_word_detected"]
    assert len(wake_events) == 3
    assert all(e["backend"] == "stub" for e in wake_events)
    assert all(e["word"] == "claudette" for e in wake_events)

    skipped = [e for e in events if e["type"] == "stt_skipped"]
    assert len(skipped) == 3

    assert events[-1]["type"] == "listener_stopped"
    assert events[-1]["emitted"] == 3


def test_stub_backend_uses_builtin_keyword_as_word_override():
    result = _run(
        "--backend", "stub",
        "--builtin-keyword", "porcupine",
        "--max-events", "1",
        "--interval", "0",
    )
    assert result.returncode == 0, result.stderr

    events = _json_lines(result.stdout)
    wake_event = next(e for e in events if e["type"] == "wake_word_detected")
    assert wake_event["word"] == "porcupine"


def test_porcupine_rejects_model_and_builtin_keyword_together():
    result = _run(
        "--backend", "porcupine",
        "--model", "fake.ppn",
        "--builtin-keyword", "porcupine",
        timeout=5,
    )
    assert result.returncode != 0
    assert "Use either --model or --builtin-keyword, not both" in result.stdout


def test_porcupine_requires_access_key():
    result = _run(
        "--backend", "porcupine",
        "--builtin-keyword", "porcupine",
        timeout=5,
    )
    assert result.returncode != 0
    assert "PORCUPINE_ACCESS_KEY not set" in result.stdout
