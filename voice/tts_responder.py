#!/usr/bin/env python3
"""
Claudette Home — TTS Responder
Reads pipeline_response events from stdin (JSON, one per line) and speaks them.

This is the last-mile audio output for the voice pipeline:
  wake_word_bridge.py | pipeline.py | tts_responder.py

Backends (tried in order of preference):
  1. OpenAI TTS API (tts-1, Sage voice) — highest quality, same as Claudette's voice
  2. gTTS (Google TTS) — free, decent quality, requires internet
  3. espeak-ng — fully offline, robotic but always works

Usage:
  # Full pipeline:
  python3 wake_word/wake_word_bridge.py | python3 pipeline.py | python3 tts_responder.py

  # Test with a single phrase (echo mode):
  echo '{"type": "pipeline_response", "text": "Done, kitchen light is off."}' | python3 tts_responder.py

  # Text mode — speak a phrase directly:
  python3 tts_responder.py --speak "Done, kitchen light is off."

  # Dry-run mode — print what would be spoken without playing:
  python3 tts_responder.py --dry-run --speak "Hello from Claudette."

  # Force a specific backend:
  python3 tts_responder.py --backend espeak --speak "Testing espeak."

Environment:
  OPENAI_API_KEY      — required for openai backend
  TTS_BACKEND         — override: openai|gtts|espeak (default: auto-detect)
  TTS_VOICE           — OpenAI voice name (default: nova — closest to Sage for tts-1)
  TTS_MODEL           — OpenAI TTS model (default: tts-1)
  TTS_SPEED           — OpenAI TTS speed (default: 1.0)
  TTS_DRY_RUN         — if set to "1", log text but don't play audio
  TTS_LANGUAGE        — language for gTTS backend (default: en)
  TTS_LANG_SLOW       — "1" to use slow mode in gTTS (default: 0)
  TTS_CACHE_DIR       — directory for caching TTS audio (default: /tmp/claudette-tts)

Audio output:
  Uses ffplay (requires ffmpeg). On systems without a display, runs with -nodisp.
  Audio goes to the default ALSA/PulseAudio output device.

Notes:
  - Non-pipeline_response events (from wake word bridge, etc.) are silently ignored.
  - If the text is empty, it's silently skipped.
  - On backend failure, falls back automatically.
"""

import argparse
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [tts_responder] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TTS_BACKEND_ENV = os.environ.get("TTS_BACKEND", "auto")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TTS_VOICE = os.environ.get("TTS_VOICE", "nova")       # nova is closest to Sage in tts-1
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")       # tts-1 for low latency, tts-1-hd for quality
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.0"))
DRY_RUN = os.environ.get("TTS_DRY_RUN", "0") == "1"
TTS_LANGUAGE = os.environ.get("TTS_LANGUAGE", "en")
TTS_CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR", "/tmp/claudette-tts"))

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def detect_backend() -> str:
    """Auto-detect the best available TTS backend."""
    if TTS_BACKEND_ENV != "auto":
        logger.info(f"Using forced backend: {TTS_BACKEND_ENV}")
        return TTS_BACKEND_ENV

    # OpenAI: needs key + requests
    if OPENAI_API_KEY:
        try:
            import requests  # noqa: F401
            logger.info("Backend: openai (API key present, requests available)")
            return "openai"
        except ImportError:
            logger.warning("OPENAI_API_KEY is set but 'requests' not installed — falling back")

    # gTTS: free Google TTS
    try:
        import gtts  # noqa: F401
        logger.info("Backend: gtts (openai unavailable)")
        return "gtts"
    except ImportError:
        pass

    # espeak-ng: always offline
    result = subprocess.run(["which", "espeak-ng"], capture_output=True)
    if result.returncode == 0:
        logger.info("Backend: espeak-ng (gtts unavailable)")
        return "espeak"

    logger.warning("No TTS backend found — falling back to print-only mode")
    return "print"


def check_ffplay() -> bool:
    """Check if ffplay is available for audio playback."""
    result = subprocess.run(["which", "ffplay"], capture_output=True)
    return result.returncode == 0


BACKEND = detect_backend()
HAS_FFPLAY = check_ffplay()

# ---------------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------------

def play_audio_file(path: str) -> bool:
    """
    Play an audio file using ffplay (silent, no display).
    Returns True on success, False on failure.
    """
    if not HAS_FFPLAY:
        logger.error("ffplay not found — cannot play audio. Install ffmpeg.")
        return False

    cmd = [
        "ffplay",
        "-nodisp",        # No video window
        "-autoexit",      # Exit when playback finishes
        "-loglevel", "error",  # Suppress noise
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.warning(f"ffplay exited with {result.returncode}: {result.stderr.decode()[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("ffplay timed out — audio too long?")
        return False
    except Exception as e:
        logger.error(f"ffplay error: {e}")
        return False


def play_audio_bytes(audio_bytes: bytes, ext: str = "mp3") -> bool:
    """Write audio bytes to a temp file and play it."""
    TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=TTS_CACHE_DIR, suffix=f".{ext}", delete=False
    ) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        return play_audio_file(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# OpenAI TTS backend
# ---------------------------------------------------------------------------

def speak_openai(text: str) -> bool:
    """
    Generate speech via OpenAI TTS API and play it.
    Uses tts-1 model (low latency) with nova voice.
    """
    try:
        import requests
    except ImportError:
        logger.error("requests not installed — run: pip install requests")
        return False

    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set")
        return False

    logger.info(f"OpenAI TTS [{TTS_MODEL}/{TTS_VOICE}]: {text!r}")
    t0 = time.time()

    try:
        resp = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": TTS_MODEL,
                "input": text,
                "voice": TTS_VOICE,
                "speed": TTS_SPEED,
                "response_format": "mp3",
            },
            timeout=20,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        logger.error(f"OpenAI TTS API error: {e} — {resp.text[:200]}")
        return False
    except requests.ConnectionError as e:
        logger.error(f"OpenAI TTS connection error: {e}")
        return False
    except requests.Timeout:
        logger.error("OpenAI TTS request timed out")
        return False

    audio_bytes = resp.content
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(f"OpenAI TTS: {len(audio_bytes)} bytes in {elapsed_ms}ms")

    return play_audio_bytes(audio_bytes, ext="mp3")


# ---------------------------------------------------------------------------
# gTTS backend
# ---------------------------------------------------------------------------

def speak_gtts(text: str) -> bool:
    """
    Generate speech via Google TTS (gTTS) and play it via ffplay.
    Requires internet. Output quality is decent.
    """
    try:
        from gtts import gTTS
    except ImportError:
        logger.error("gtts not installed — run: pip install gtts")
        return False

    logger.info(f"gTTS [{TTS_LANGUAGE}]: {text!r}")
    t0 = time.time()

    try:
        tts = gTTS(text=text, lang=TTS_LANGUAGE, slow=False)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        audio_bytes = buf.getvalue()
    except Exception as e:
        logger.error(f"gTTS error: {e}")
        return False

    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(f"gTTS: {len(audio_bytes)} bytes in {elapsed_ms}ms")

    return play_audio_bytes(audio_bytes, ext="mp3")


# ---------------------------------------------------------------------------
# espeak-ng backend
# ---------------------------------------------------------------------------

def speak_espeak(text: str) -> bool:
    """
    Speak using espeak-ng — fully offline, robotic but always works.
    Good as an emergency fallback.
    """
    logger.info(f"espeak-ng: {text!r}")
    try:
        result = subprocess.run(
            ["espeak-ng", "-v", "en-us", "-s", "150", text],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning(f"espeak-ng exited {result.returncode}: {result.stderr.decode()[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("espeak-ng timed out")
        return False
    except FileNotFoundError:
        logger.error("espeak-ng not found")
        return False
    except Exception as e:
        logger.error(f"espeak-ng error: {e}")
        return False


# ---------------------------------------------------------------------------
# Print-only fallback
# ---------------------------------------------------------------------------

def speak_print(text: str) -> bool:
    """Last resort: just print the text (no audio)."""
    print(f"[TTS] {text}", flush=True)
    return True


# ---------------------------------------------------------------------------
# Main speak() dispatcher with fallback chain
# ---------------------------------------------------------------------------

def speak(text: str, backend: Optional[str] = None, dry_run: bool = False) -> bool:
    """
    Speak a text string using the best available TTS backend.

    Args:
        text: Text to speak
        backend: Override backend (openai|gtts|espeak|print). None = use BACKEND global.
        dry_run: If True, log what would be spoken but don't play audio.

    Returns:
        True if spoken (or logged in dry-run), False on complete failure.
    """
    if not text or not text.strip():
        logger.debug("Empty text — skipping")
        return True

    text = text.strip()

    if dry_run or DRY_RUN:
        logger.info(f"[DRY RUN] Would speak: {text!r}")
        print(f"[DRY RUN TTS] {text}", flush=True)
        return True

    chosen_backend = backend or BACKEND

    # Try the chosen backend first, then fall through the chain
    backends_to_try = [chosen_backend]
    full_chain = ["openai", "gtts", "espeak", "print"]
    for b in full_chain:
        if b not in backends_to_try:
            backends_to_try.append(b)

    for b in backends_to_try:
        logger.info(f"Trying TTS backend: {b}")
        try:
            if b == "openai":
                ok = speak_openai(text)
            elif b == "gtts":
                ok = speak_gtts(text)
            elif b == "espeak":
                ok = speak_espeak(text)
            else:
                ok = speak_print(text)
        except Exception as e:
            logger.warning(f"Backend {b} raised exception: {e}")
            ok = False

        if ok:
            if b != chosen_backend:
                logger.info(f"Used fallback backend: {b}")
            return True
        else:
            logger.warning(f"Backend {b} failed — trying next")

    logger.error("All TTS backends failed")
    return False


# ---------------------------------------------------------------------------
# Pipeline stdin reader
# ---------------------------------------------------------------------------

def run_from_stdin(dry_run: bool = False):
    """
    Read JSON events from stdin. On pipeline_response events, speak the text.
    All other event types are ignored (they come from wake_word_bridge + pipeline).

    This is the main mode when piped:
      wake_word_bridge.py | pipeline.py | tts_responder.py
    """
    logger.info(f"TTS Responder started. Backend: {BACKEND}. Waiting for pipeline_response events...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        # Try to parse as JSON — if not, pass through
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Not JSON — could be a debug log from upstream; pass through
            logger.debug(f"Non-JSON line: {line!r}")
            continue

        event_type = event.get("type")

        if event_type == "pipeline_response":
            text = event.get("text", "").strip()
            if text:
                logger.info(f"Speaking: {text!r}")
                speak(text, dry_run=dry_run)
            else:
                logger.debug("pipeline_response with empty text — skipping")

        elif event_type in ("wake_word_detected", "listener_started", "listener_stopped"):
            # Pass these through as JSON for any further downstream consumers
            print(line, flush=True)

        elif event_type == "error":
            logger.error(f"Upstream error event: {event}")
            # Optionally speak the error aloud for in-room awareness
            msg = event.get("message", "")
            if msg:
                speak(f"Error: {msg}", dry_run=dry_run)

        else:
            # Unknown event type — pass through silently
            logger.debug(f"Unknown event type: {event_type!r}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Claudette Home TTS Responder — speaks pipeline_response events",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline:
  python3 wake_word/wake_word_bridge.py | python3 pipeline.py | python3 tts_responder.py

  # Test with a single event:
  echo '{"type":"pipeline_response","text":"Kitchen light is off."}' | python3 tts_responder.py

  # Speak directly:
  python3 tts_responder.py --speak "Goodnight, Mattie."

  # Dry run (no audio):
  python3 tts_responder.py --dry-run --speak "Testing."

  # Force backend:
  python3 tts_responder.py --backend espeak --speak "Offline voice check."
        """,
    )
    parser.add_argument("--speak", metavar="TEXT", help="Speak TEXT directly and exit")
    parser.add_argument(
        "--backend",
        choices=["openai", "gtts", "espeak", "print", "auto"],
        default="auto",
        help="Force TTS backend (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        default=os.environ.get("TTS_DRY_RUN", "0") == "1",
        help="Log what would be spoken but don't play audio",
    )
    parser.add_argument(
        "--list-backends", action="store_true",
        help="Show available backends and exit",
    )
    args = parser.parse_args()

    if args.list_backends:
        print("TTS backends:")
        print(f"  openai    {'✅ OPENAI_API_KEY set' if OPENAI_API_KEY else '❌ OPENAI_API_KEY not set'}")
        try:
            import gtts  # noqa: F401
            gtts_status = "✅ installed"
        except ImportError:
            gtts_status = "❌ not installed (pip install gtts)"
        print(f"  gtts      {gtts_status}")
        espeak_result = subprocess.run(["which", "espeak-ng"], capture_output=True)
        print(f"  espeak-ng {'✅ found at ' + espeak_result.stdout.decode().strip() if espeak_result.returncode == 0 else '❌ not found'}")
        print(f"  ffplay    {'✅ found' if HAS_FFPLAY else '❌ not found (install ffmpeg)'}")
        print(f"\nAuto-detected backend: {BACKEND}")
        return

    # Override backend if specified
    backend_override = None if args.backend == "auto" else args.backend

    if args.speak:
        ok = speak(args.speak, backend=backend_override, dry_run=args.dry_run)
        sys.exit(0 if ok else 1)
    else:
        if backend_override:
            # Patch the module-level BACKEND for stdin mode
            import tts_responder as _self
            _self.BACKEND = backend_override
        run_from_stdin(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
