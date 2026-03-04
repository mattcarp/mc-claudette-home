#!/usr/bin/env python3
"""
Claudette Home — Voice Pipeline Orchestrator
Chains: wake word → STT → intent parser → HA bridge → (TTS response)

This is the glue that connects all four modules into a working voice pipeline.
Run this on the Workshop. It listens for wake words, sends audio to the STT
API, parses the intent, and fires the HA action.

Architecture:
  [wake_word_bridge.py stdout] ─→ pipeline.py reads JSON events
         ↓ wake_word_detected
  [record_audio()] ─→ PCM WAV bytes
         ↓
  [STT API: POST http://localhost:8765/transcribe]
         ↓ {"text": "turn off the kitchen light"}
  [intent_parser.parse_intent(text)]
         ↓ {"action": "call_service", "domain": "light", ...}
  [ha_bridge.execute_action(action)]
         ↓ HA executes
  [TTS response via OpenClaw / sag / tts tool]

Usage:
  # Full pipeline (requires mic + STT API running + HA running)
  python3 pipeline.py

  # Stub mode — no hardware needed, tests full logic chain
  python3 pipeline.py --stub

  # Text-only mode (skip wake word + STT, use typed input)
  python3 pipeline.py --text "turn off the living room lights"

  # Test specific transcript against intent parser + HA (no audio)
  python3 pipeline.py --text "I'm going to bed" --stub

Environment:
  # Pipeline mode
  PIPELINE_STUB_MODE=1     — enable stub mode (no hardware, no HA)
  PIPELINE_TEXT_MODE=1     — skip wake word + STT, read from stdin

  # STT API
  STT_API_URL=http://localhost:8765   — where transcribe_api is running
  STT_API_KEY=...                     — if auth is enabled on transcribe_api

  # HA Bridge
  HA_URL=http://localhost:8123
  HA_TOKEN=eyJ...

  # Wake word
  WAKE_WORD_BACKEND=porcupine|oww
  PORCUPINE_ACCESS_KEY=...
  WAKE_WORD_MODEL=voice/wake_word/models/claudette_linux.ppn

  # Audio recording
  RECORD_SECONDS=5                    — how long to record after wake word (default: 5)
  RECORD_SAMPLE_RATE=16000
"""

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional, Union

# Add sibling dirs to path so we can import our modules
VOICE_DIR = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(VOICE_DIR, "intent_parser"))
sys.path.insert(0, os.path.join(VOICE_DIR, "ha_bridge"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [pipeline] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STUB_MODE = os.environ.get("PIPELINE_STUB_MODE", "0") == "1"
STT_API_URL = os.environ.get("STT_API_URL", "http://localhost:8765")
STT_API_KEY = os.environ.get("STT_API_KEY", "")
RECORD_SECONDS = int(os.environ.get("RECORD_SECONDS", "5"))
RECORD_SAMPLE_RATE = int(os.environ.get("RECORD_SAMPLE_RATE", "16000"))


# ---------------------------------------------------------------------------
# Audio recording
# ---------------------------------------------------------------------------

def record_audio_pyaudio(seconds: int = RECORD_SECONDS, sample_rate: int = RECORD_SAMPLE_RATE) -> bytes:
    """
    Record audio from the default mic using PyAudio.
    Returns raw WAV bytes (16kHz, mono, 16-bit PCM).
    """
    try:
        import pyaudio
        import wave
    except ImportError:
        raise ImportError("pyaudio not installed — run: pip install pyaudio")

    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=sample_rate,
        input=True,
        frames_per_buffer=CHUNK,
    )

    logger.info(f"Recording {seconds}s of audio at {sample_rate}Hz...")
    frames = []
    for _ in range(0, int(sample_rate / CHUNK * seconds)):
        data = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)

    stream.stop_stream()
    stream.close()
    pa.terminate()

    # Encode as WAV
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(pa.get_sample_size(FORMAT))
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(frames))

    logger.info(f"Recorded {len(buf.getvalue())} bytes of WAV audio")
    return buf.getvalue()


def record_audio_arecord(seconds: int = RECORD_SECONDS, sample_rate: int = RECORD_SAMPLE_RATE) -> bytes:
    """
    Record audio using arecord (ALSA CLI — fallback on Linux if PyAudio fails).
    Returns raw WAV bytes.
    """
    logger.info(f"Recording {seconds}s via arecord...")
    result = subprocess.run(
        ["arecord", "-f", "S16_LE", "-r", str(sample_rate), "-c", "1",
         "-d", str(seconds), "--quiet", "-t", "wav", "/dev/stdout"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"arecord failed: {result.stderr.decode()}")
    return result.stdout


def record_audio(seconds: int = RECORD_SECONDS) -> bytes:
    """Record audio — tries PyAudio first, falls back to arecord."""
    try:
        return record_audio_pyaudio(seconds)
    except ImportError:
        logger.warning("PyAudio not available, falling back to arecord")
        return record_audio_arecord(seconds)


def record_audio_stub(seconds: int = RECORD_SECONDS) -> bytes:
    """Stub — returns minimal WAV bytes for testing without a mic."""
    import wave
    buf = io.BytesIO()
    sample_rate = 16000
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Write 0.5s of silence
        wf.writeframes(b"\x00\x00" * (sample_rate // 2))
    logger.info("[STUB] Generated silent WAV bytes (no mic)")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# STT — call transcribe_api
# ---------------------------------------------------------------------------

def transcribe(audio_bytes: bytes, stub: bool = False) -> str:
    """
    Send audio to the STT API and return the transcript text.

    Args:
        audio_bytes: WAV audio bytes from record_audio()
        stub: if True, return a stub transcript

    Returns:
        Transcript string (e.g. "turn off the living room lights")
    """
    if stub:
        logger.info("[STUB] Skipping STT API — returning stub transcript")
        return "turn off the living room lights"

    try:
        import requests
    except ImportError:
        raise ImportError("requests not installed — run: pip install requests")

    headers = {}
    if STT_API_KEY:
        headers["Authorization"] = f"Bearer {STT_API_KEY}"

    logger.info(f"Sending {len(audio_bytes)} bytes to STT API: {STT_API_URL}/transcribe")
    t0 = time.time()

    try:
        r = requests.post(
            f"{STT_API_URL}/transcribe",
            files={"audio": ("audio.wav", io.BytesIO(audio_bytes), "audio/wav")},
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
    except requests.ConnectionError:
        raise RuntimeError(
            f"STT API not running at {STT_API_URL}. "
            "Start it with: uvicorn transcribe_api:app --host 0.0.0.0 --port 8765"
        )

    result = r.json()
    text = result.get("text", "").strip()
    mode = result.get("model", "?")
    duration_ms = result.get("duration_ms", 0)

    logger.info(f"STT result [{mode}] in {duration_ms}ms: {text!r}")
    return text


# ---------------------------------------------------------------------------
# Intent → HA
# ---------------------------------------------------------------------------

def handle_transcript(transcript: str, bridge, stub: bool = False) -> dict:
    """
    Parse a transcript and execute the HA action.

    Args:
        transcript: Spoken text from STT
        bridge: HABridge or HABridgeStub instance
        stub: If True, uses SAMPLE_ENTITIES (no live HA)

    Returns:
        Result dict with action taken and HA response
    """
    logger.info(f"Parsing intent: {transcript!r}")

    try:
        from intent_parser import parse_intent, format_action_summary
    except ImportError as e:
        raise ImportError(f"Cannot import intent_parser: {e}")

    # Use live entities if bridge has them; fall back to SAMPLE_ENTITIES
    entities = None
    if not stub:
        try:
            entities = bridge.get_entities()
            logger.info(f"Fetched {sum(len(v) for v in entities.values())} entities from HA")
        except Exception as e:
            logger.warning(f"Failed to fetch live entities, using SAMPLE_ENTITIES: {e}")

    # Parse the intent
    try:
        action = parse_intent(transcript, entities=entities)
    except EnvironmentError as e:
        logger.error(f"Intent parser error: {e}")
        return {"error": str(e), "transcript": transcript}
    except ValueError as e:
        logger.error(f"Intent parse failed: {e}")
        return {"error": str(e), "transcript": transcript}

    logger.info(f"Parsed action: {json.dumps(action)}")

    # Execute the action
    results = bridge.execute_action(action)
    logger.info(f"HA result: {json.dumps(results)}")

    # Build response text for TTS
    response_text = build_response(action, results)
    logger.info(f"TTS response: {response_text!r}")

    return {
        "transcript": transcript,
        "action": action,
        "results": results,
        "response": response_text,
    }


def build_response(action: Union[dict, list], results: list) -> str:
    """
    Build a natural-language TTS response for Claudette to speak.
    Keeps it short — confirmation or clarification.
    """
    if isinstance(action, list):
        # Multi-action: summarise count
        count = len(action)
        return f"Done, {count} actions completed."

    action_type = action.get("action")

    if action_type == "clarify":
        return action.get("question", "Could you clarify?")

    elif action_type == "query":
        # Relay the state value
        if results and results[0].get("ok"):
            state = results[0].get("state", "unknown")
            entity_id = action.get("entity_id", "")
            return f"The {entity_id.replace('_', ' ').replace('.', ' ')} is {state}."
        return "I couldn't get that reading."

    elif action_type == "call_service":
        domain = action.get("domain", "")
        service = action.get("service", "")
        entity_id = action.get("entity_id", "")
        friendly = entity_id.replace("light.", "").replace("switch.", "").replace("scene.", "").replace("_", " ")

        if service == "turn_on":
            return f"Done, {friendly} is on."
        elif service == "turn_off":
            return f"Done, {friendly} is off."
        elif service == "toggle":
            return f"Toggled {friendly}."
        elif service == "lock":
            return f"Locked."
        elif service == "unlock":
            return f"Unlocked."
        elif service in ("open", "close"):
            return f"Shutters {service}d."
        elif service == "activate":
            return f"{friendly.title()} scene activated."
        elif service == "set_temperature":
            temp = action.get("params", {}).get("temperature", "")
            return f"Temperature set to {temp}."
        else:
            return "Done."

    return "Done."


# ---------------------------------------------------------------------------
# Wake word event loop (reads stdout from wake_word_bridge.py)
# ---------------------------------------------------------------------------

def run_pipeline_from_stdin(stub: bool = False):
    """
    Main pipeline loop: reads JSON events from stdin (piped from wake_word_bridge.py).
    On wake_word_detected: records audio → STT → intent → HA.

    Usage:
      python3 wake_word/wake_word_bridge.py | python3 pipeline.py

    In stub mode:
      echo '{"type":"wake_word_detected","word":"claudette","backend":"stub"}' | python3 pipeline.py --stub
    """
    from ha_bridge import get_bridge
    bridge = get_bridge(stub=stub)

    logger.info(f"Pipeline started. Waiting for wake word events... (stub={stub})")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(f"Non-JSON line from wake word bridge: {line!r}")
            continue

        event_type = event.get("type")

        if event_type == "wake_word_detected":
            logger.info(f"Wake word detected: {event.get('word')} (backend={event.get('backend')})")
            _handle_wake_word(bridge, stub)

        elif event_type == "listener_started":
            logger.info(f"Wake word listener started: {event}")

        elif event_type == "listener_stopped":
            logger.info("Wake word listener stopped")
            break

        elif event_type == "error":
            logger.error(f"Wake word error: {event}")


def _handle_wake_word(bridge, stub: bool):
    """React to a wake word event: record → transcribe → parse → execute."""
    # Record
    try:
        if stub:
            audio = record_audio_stub()
        else:
            audio = record_audio()
    except Exception as e:
        logger.error(f"Audio recording failed: {e}")
        return

    # Transcribe
    try:
        transcript = transcribe(audio, stub=stub)
    except Exception as e:
        logger.error(f"STT failed: {e}")
        return

    if not transcript:
        logger.info("Empty transcript — ignoring")
        return

    # Intent + HA
    result = handle_transcript(transcript, bridge, stub=stub)
    response = result.get("response", "")
    if response:
        logger.info(f"Response: {response}")
        # TODO: TTS — pipe response to sag/tts or OpenClaw's tts endpoint
        # For now, just print — the systemd unit can pipe to tts separately
        print(json.dumps({"type": "pipeline_response", "text": response}), flush=True)


# ---------------------------------------------------------------------------
# Text mode — bypass wake word + STT, type a command
# ---------------------------------------------------------------------------

def run_text_mode(text: str, stub: bool = False):
    """
    Shortcut: skip wake word and STT, directly parse a typed command.
    Good for testing intent parser + HA bridge from the CLI.
    """
    from ha_bridge import get_bridge
    bridge = get_bridge(stub=stub)

    result = handle_transcript(text, bridge, stub=stub)
    print(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# Systemd service file
# ---------------------------------------------------------------------------

SERVICE_UNIT = """\
[Unit]
Description=Claudette Home — Voice Pipeline
After=network.target claudette-wake-word.service
Wants=claudette-wake-word.service

[Service]
Type=simple
User=sysop
WorkingDirectory=/home/sysop/projects/mc-home/voice
# Pipe wake word bridge stdout into pipeline
ExecStart=/bin/bash -c 'python3 wake_word/wake_word_bridge.py | python3 pipeline.py'
Restart=on-failure
RestartSec=5
EnvironmentFile=/etc/environment
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def write_service_file(path: str = "/etc/systemd/system/claudette-pipeline.service"):
    """Write the systemd service unit file."""
    with open(path, "w") as f:
        f.write(SERVICE_UNIT)
    print(f"Written: {path}")
    print("To install:")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable claudette-pipeline")
    print("  sudo systemctl start claudette-pipeline")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Claudette Home — Voice Pipeline Orchestrator"
    )
    parser.add_argument(
        "--stub", action="store_true",
        default=os.environ.get("PIPELINE_STUB_MODE", "0") == "1",
        help="Run in stub mode (no mic, no HA — dev/testing)"
    )
    parser.add_argument(
        "--text", metavar="TRANSCRIPT",
        help="Skip wake word + STT; parse this text directly (text mode)"
    )
    parser.add_argument(
        "--write-service", action="store_true",
        help="Write systemd service file to /etc/systemd/system/claudette-pipeline.service"
    )
    args = parser.parse_args()

    if args.write_service:
        write_service_file()
        return

    if args.text:
        run_text_mode(args.text, stub=args.stub)
    else:
        # Read wake word events from stdin (piped from wake_word_bridge.py)
        run_pipeline_from_stdin(stub=args.stub)


if __name__ == "__main__":
    main()
