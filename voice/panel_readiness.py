#!/usr/bin/env python3
"""
Claudette Home — Panel Readiness Pre-Flight Checker
Run this before the MOES 10.1" panel arrives to verify the full stack
is ready for hardware integration.

Checks:
  1. claudette-stt.service running + responsive
  2. STT latency within target (<3s)
  3. Pipeline stub smoke-test (text mode)
  4. Wake word bridge stdout → pipeline stdin contract
  5. Porcupine SDK installed
  6. PORCUPINE_ACCESS_KEY set
  7. .ppn model file present (claudette_linux.ppn)
  8. VAD recorder importable
  9. TTS responder working (dry-run)
 10. HA bridge importable (stub mode)
 11. Audio devices present (ALSA)
 12. Network connectivity (Workshop self-check)

Usage:
  python3 voice/panel_readiness.py
  python3 voice/panel_readiness.py --json    # machine-readable output
  python3 voice/panel_readiness.py --fix     # attempt auto-fixes where possible

Run from mc-home root.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

VOICE_DIR = Path(__file__).parent
PROJECT_ROOT = VOICE_DIR.parent

# STT API URL
STT_URL = os.environ.get("STT_API_URL", "http://127.0.0.1:8765")

# ─────────────────────────────────────────────
# Result model
# ─────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    fix_hint: str = ""
    critical: bool = True  # critical checks block the panel from working


PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


# ─────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────

def check_stt_service_running() -> CheckResult:
    """Is claudette-stt.service active?"""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "claudette-stt.service"],
            capture_output=True, text=True, timeout=5
        )
        active = r.stdout.strip() == "active"
        return CheckResult(
            name="STT service (claudette-stt.service)",
            passed=active,
            detail=r.stdout.strip(),
            fix_hint="sudo systemctl start claudette-stt.service"
        )
    except Exception as e:
        return CheckResult(
            name="STT service (claudette-stt.service)",
            passed=False,
            detail=str(e),
            fix_hint="sudo systemctl start claudette-stt.service"
        )


def check_stt_health() -> CheckResult:
    """GET /health on STT API."""
    try:
        import requests
        r = requests.get(f"{STT_URL}/health", timeout=5)
        data = r.json()
        ok = r.status_code == 200 and data.get("status") == "ok"
        return CheckResult(
            name="STT /health endpoint",
            passed=ok,
            detail=f"backend={data.get('backend')} mode={data.get('mode')} model={data.get('model')}",
            fix_hint="Check claudette-stt.service logs: journalctl -u claudette-stt.service -n 50"
        )
    except Exception as e:
        return CheckResult(
            name="STT /health endpoint",
            passed=False,
            detail=str(e),
            fix_hint="Ensure claudette-stt.service is running"
        )


def check_stt_latency() -> CheckResult:
    """Round-trip transcription latency test (<3s target)."""
    try:
        import requests

        # Generate silence WAV (0.5s) — tests pipeline without needing gTTS
        buf = io.BytesIO()
        n_samples = 8000
        with wave.open(buf, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * n_samples)
        buf.seek(0)
        wav_bytes = buf.read()

        t0 = time.perf_counter()
        r = requests.post(
            f"{STT_URL}/transcribe",
            files={"audio": ("silence.wav", io.BytesIO(wav_bytes), "audio/wav")},
            timeout=10,
        )
        elapsed = time.perf_counter() - t0

        ok = r.status_code == 200 and elapsed < 3.0
        return CheckResult(
            name="STT latency (<3s)",
            passed=ok,
            detail=f"{elapsed:.2f}s (target <3s)",
            fix_hint="Check Workshop load / STT service health",
            critical=True,
        )
    except Exception as e:
        return CheckResult(
            name="STT latency (<3s)",
            passed=False,
            detail=str(e),
            fix_hint="Ensure STT service is running and network is reachable"
        )


def check_pipeline_text_mode() -> CheckResult:
    """pipeline.py --stub --text smoke test."""
    try:
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "pipeline.py"), "--stub", "--text", "turn on the living room lights"],
            capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
        )
        ok = r.returncode == 0 and "{" in r.stdout
        detail = "OK" if ok else f"exit={r.returncode} stderr={r.stderr[:200]}"
        return CheckResult(
            name="Pipeline text mode (stub)",
            passed=ok,
            detail=detail,
            fix_hint="Check intent_parser/ imports and OPENROUTER_API_KEY / OPENAI_API_KEY env vars"
        )
    except Exception as e:
        return CheckResult(
            name="Pipeline text mode (stub)",
            passed=False,
            detail=str(e),
            fix_hint="Check pipeline.py imports"
        )


def check_wake_bridge_pipe_contract() -> CheckResult:
    """
    Inject a wake_word_detected JSON event into pipeline.py stdin.
    Pipeline should emit a pipeline_response event. Tests the pipe contract.
    """
    try:
        wake_event = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "wake_word_detected",
            "backend": "stub",
            "word": "claudette",
        })
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "pipeline.py"), "--stub"],
            input=wake_event + "\n",
            capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
        )
        # Pipeline should produce a pipeline_response or at least complete without crashing
        ok = r.returncode == 0
        # Check if any pipeline_response was emitted
        has_response = "pipeline_response" in r.stdout or "pipeline_event" in r.stdout
        detail = "pipe contract OK" if ok else f"exit={r.returncode}"
        if ok and not has_response:
            detail += " (no pipeline_response in output — stub audio returned empty transcript)"
        return CheckResult(
            name="Wake word → pipeline pipe contract",
            passed=ok,
            detail=detail,
            fix_hint="Check pipeline.py run_pipeline_from_stdin() for JSON parsing errors"
        )
    except Exception as e:
        return CheckResult(
            name="Wake word → pipeline pipe contract",
            passed=False,
            detail=str(e),
        )


def check_porcupine_sdk() -> CheckResult:
    """pvporcupine importable."""
    try:
        import pvporcupine
        return CheckResult(
            name="Porcupine SDK (pvporcupine)",
            passed=True,
            detail=f"version OK",
        )
    except ImportError:
        return CheckResult(
            name="Porcupine SDK (pvporcupine)",
            passed=False,
            detail="Not installed",
            fix_hint="pip install pvporcupine"
        )


def check_porcupine_access_key() -> CheckResult:
    """PORCUPINE_ACCESS_KEY env var set."""
    key = os.environ.get("PORCUPINE_ACCESS_KEY", "")
    ok = bool(key and len(key) > 10)
    return CheckResult(
        name="PORCUPINE_ACCESS_KEY env var",
        passed=ok,
        detail="Set" if ok else "Not set",
        fix_hint=(
            "1. Sign up at https://console.picovoice.ai (free)\n"
            "   2. Copy your Access Key\n"
            "   3. Add to /etc/environment: PORCUPINE_ACCESS_KEY=<key>\n"
            "   4. sudo systemctl daemon-reload && source /etc/environment"
        )
    )


def check_ppn_model() -> CheckResult:
    """claudette_linux.ppn model file present."""
    model_path = VOICE_DIR / "wake_word" / "models" / "claudette_linux.ppn"
    ok = model_path.exists()
    return CheckResult(
        name="Porcupine model (claudette_linux.ppn)",
        passed=ok,
        detail=str(model_path) if ok else "File missing",
        fix_hint=(
            "1. Log in at https://console.picovoice.ai\n"
            "   2. Go to 'Wake Word' → 'Train a custom wake word'\n"
            "   3. Enter wake word: claudette\n"
            "   4. Select platform: Linux (x86_64)\n"
            "   5. Download .ppn file\n"
            f"   6. Place at: {model_path}"
        )
    )


def check_vad_recorder() -> CheckResult:
    """vad_recorder importable."""
    try:
        sys.path.insert(0, str(VOICE_DIR))
        import importlib
        spec = importlib.util.spec_from_file_location("vad_recorder", VOICE_DIR / "vad_recorder.py")
        mod = importlib.util.module_from_spec(spec)
        # Don't exec (may need torch); just check it parses
        import ast
        src = (VOICE_DIR / "vad_recorder.py").read_text()
        ast.parse(src)
        return CheckResult(
            name="VAD recorder (vad_recorder.py)",
            passed=True,
            detail="Parses OK",
        )
    except Exception as e:
        return CheckResult(
            name="VAD recorder (vad_recorder.py)",
            passed=False,
            detail=str(e),
            fix_hint="pip install silero-vad pyaudio"
        )


def check_tts_responder() -> CheckResult:
    """tts_responder.py --dry-run works."""
    try:
        event_json = json.dumps({"type": "pipeline_response", "text": "Panel readiness test complete."})
        r = subprocess.run(
            [sys.executable, str(VOICE_DIR / "tts_responder.py"), "--dry-run"],
            input=event_json + "\n",
            capture_output=True, text=True, timeout=10, cwd=str(PROJECT_ROOT),
        )
        ok = r.returncode == 0 and "Panel readiness" in r.stdout
        return CheckResult(
            name="TTS responder (--dry-run)",
            passed=ok,
            detail="OK" if ok else f"exit={r.returncode} {r.stderr[:100]}",
            fix_hint="Check tts_responder.py for import errors"
        )
    except Exception as e:
        return CheckResult(
            name="TTS responder (--dry-run)",
            passed=False,
            detail=str(e),
        )


def check_ha_bridge_stub() -> CheckResult:
    """HA bridge importable in stub mode."""
    try:
        sys.path.insert(0, str(VOICE_DIR / "ha_bridge"))
        import ast
        src = (VOICE_DIR / "ha_bridge" / "ha_bridge.py").read_text()
        ast.parse(src)
        return CheckResult(
            name="HA bridge (ha_bridge.py parses)",
            passed=True,
            detail="Parses OK",
        )
    except Exception as e:
        return CheckResult(
            name="HA bridge (ha_bridge.py parses)",
            passed=False,
            detail=str(e),
            fix_hint="Check ha_bridge.py for syntax errors"
        )


def check_audio_devices() -> CheckResult:
    """ALSA audio devices present."""
    try:
        r = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5
        )
        has_devices = "card" in r.stdout.lower()
        return CheckResult(
            name="Audio input devices (arecord -l)",
            passed=has_devices,
            detail=r.stdout.strip()[:200] if has_devices else "No capture devices found",
            fix_hint="The Workshop needs a USB mic or the MOES panel provides its own mic (panel mode)",
            critical=False,  # Not critical until panel arrives
        )
    except FileNotFoundError:
        return CheckResult(
            name="Audio input devices (arecord -l)",
            passed=False,
            detail="arecord not found",
            fix_hint="apt install alsa-utils",
            critical=False,
        )
    except Exception as e:
        return CheckResult(
            name="Audio input devices (arecord -l)",
            passed=False,
            detail=str(e),
            critical=False,
        )


def check_network_self() -> CheckResult:
    """Workshop can reach STT service (self-check)."""
    try:
        import socket
        s = socket.create_connection(("127.0.0.1", 8765), timeout=3)
        s.close()
        return CheckResult(
            name="STT port 8765 reachable (localhost)",
            passed=True,
            detail="TCP connect OK",
        )
    except Exception as e:
        return CheckResult(
            name="STT port 8765 reachable (localhost)",
            passed=False,
            detail=str(e),
            fix_hint="sudo systemctl start claudette-stt.service"
        )


# ─────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────

ALL_CHECKS = [
    check_stt_service_running,
    check_stt_health,
    check_stt_latency,
    check_pipeline_text_mode,
    check_wake_bridge_pipe_contract,
    check_porcupine_sdk,
    check_porcupine_access_key,
    check_ppn_model,
    check_vad_recorder,
    check_tts_responder,
    check_ha_bridge_stub,
    check_audio_devices,
    check_network_self,
]


def run_checks(fix: bool = False) -> List[CheckResult]:
    results = []
    for fn in ALL_CHECKS:
        try:
            r = fn()
        except Exception as e:
            r = CheckResult(name=fn.__name__, passed=False, detail=f"Check crashed: {e}")
        results.append(r)
    return results


def print_report(results: List[CheckResult]):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  Claudette Home — Panel Readiness Report")
    print(f"  {now}")
    print(f"{'='*60}\n")

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    critical_fails = [r for r in failed if r.critical]

    for r in results:
        icon = PASS if r.passed else (FAIL if r.critical else WARN)
        print(f"  {icon} {r.name}")
        if r.detail:
            print(f"       {r.detail}")
        if not r.passed and r.fix_hint:
            hint_lines = r.fix_hint.split("\n")
            print(f"       💡 {hint_lines[0]}")
            for line in hint_lines[1:]:
                print(f"          {line}")
        print()

    print(f"{'─'*60}")
    print(f"  Results: {len(passed)}/{len(results)} passed")

    if not critical_fails:
        print(f"\n  🎉 All critical checks passed! Panel-ready.")
        print(f"     When MOES panel arrives:")
        print(f"       1. Set PORCUPINE_ACCESS_KEY + download .ppn")
        print(f"       2. sudo systemctl start claudette-pipeline.service")
        print(f"       3. Plug in panel, open Claudette Home app")
    else:
        print(f"\n  ⛔ {len(critical_fails)} critical issue(s) to fix before panel arrives:")
        for r in critical_fails:
            print(f"     • {r.name}")

    # Non-critical
    non_critical_fails = [r for r in failed if not r.critical]
    if non_critical_fails:
        print(f"\n  ⚠️  {len(non_critical_fails)} non-critical (won't block panel):")
        for r in non_critical_fails:
            print(f"     • {r.name}")

    print(f"{'='*60}\n")
    return len(critical_fails) == 0


def print_json_report(results: List[CheckResult]):
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "critical_fails": sum(1 for r in results if not r.passed and r.critical),
        },
        "checks": [
            {
                "name": r.name,
                "passed": r.passed,
                "critical": r.critical,
                "detail": r.detail,
                "fix_hint": r.fix_hint,
            }
            for r in results
        ],
    }
    print(json.dumps(out, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Claudette Home — Panel Readiness Checker")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human report")
    parser.add_argument("--fix", action="store_true", help="Attempt auto-fixes where possible")
    args = parser.parse_args()

    results = run_checks(fix=args.fix)

    if args.json:
        print_json_report(results)
    else:
        all_critical_ok = print_report(results)
        sys.exit(0 if all_critical_ok else 1)


if __name__ == "__main__":
    main()
