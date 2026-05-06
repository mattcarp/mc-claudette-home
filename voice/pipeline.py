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
  WAKE_WORD_BACKEND=porcupine|oww|stub
  PORCUPINE_ACCESS_KEY=...
  WAKE_WORD_MODEL=voice/wake_word/models/claudette_linux.ppn
  WAKE_WORD_MAX_EVENTS=1              — stub mode: finite wake events for CI/dev
  WAKE_WORD_STUB_INTERVAL=0.1         — stub mode: seconds between emitted events

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
PROJECT_ROOT = os.path.dirname(VOICE_DIR)
sys.path.insert(0, os.path.join(VOICE_DIR, "intent_parser"))
sys.path.insert(0, os.path.join(VOICE_DIR, "ha_bridge"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
    """Record audio — tries VAD-based recording first, then PyAudio, then arecord."""
    try:
        from vad_recorder import create_vad_recorder, VadConfig
        config = VadConfig(max_duration=float(seconds))
        recorder = create_vad_recorder(config)
        result = recorder.record()
        logger.info(
            f"VAD recording: speech={result.speech_detected}, "
            f"speech_ms={result.speech_duration_ms:.0f}, "
            f"ended_by_silence={result.ended_by_silence}"
        )
        return result.audio_bytes
    except Exception as e:
        logger.warning(f"VAD recorder not available ({e}), falling back to fixed-time recording")
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

def _init_scene_scheduler():
    """Initialize the scene scheduler. Returns SceneScheduler or None."""
    try:
        from brain.scene_scheduler import SceneScheduler, voice_to_scene, OVERRIDE_CLEAR
        scheduler = SceneScheduler()
        logger.info("Scene scheduler initialized")
        return scheduler
    except Exception as e:
        logger.warning(f"Scene scheduler unavailable: {e}")
        return None


def _handle_scene_trigger(transcript: str, scheduler, bridge) -> Optional[dict]:
    """
    Check if a transcript is a scene voice trigger and handle it.

    Returns a result dict if handled, or None if it should fall through to the intent parser.
    """
    from brain.scene_scheduler import voice_to_scene, OVERRIDE_CLEAR

    scene_name = voice_to_scene(transcript)
    if scene_name is None:
        return None  # No match — let intent parser handle it

    if scene_name == OVERRIDE_CLEAR:
        scheduler._clear_override()
        response = "Welcome home! Auto schedule resumed."
        logger.info(f"Scene override cleared via voice trigger: {transcript!r}")
        return {
            "transcript": transcript,
            "action": {"action": "clear_override"},
            "results": [],
            "response": response,
        }

    # Activate the scene through the scheduler
    result = scheduler.activate(scene_name, source="voice")
    scene_actions = result.get("actions", [])

    # Execute scene actions through HA bridge
    ha_results = []
    for act in scene_actions:
        action_dict = {
            "action": "call_service",
            "domain": act["domain"],
            "service": act["service"],
            "entity_id": act["entity_id"],
            "params": act.get("params", {}),
        }
        try:
            r = bridge.execute_action(action_dict)
            ha_results.append(r if isinstance(r, dict) else {"ok": True})
        except Exception as e:
            ha_results.append({"error": str(e)})
            logger.warning(f"Scene action failed: {act} — {e}")

    response = build_scene_response(scene_name, ha_results)
    logger.info(f"Scene '{scene_name}' activated via voice trigger: {transcript!r}")

    return {
        "transcript": transcript,
        "action": {"action": "activate_scene", "scene": scene_name},
        "results": ha_results,
        "response": response,
    }


def handle_transcript(transcript: str, bridge, stub: bool = False, scheduler=None) -> dict:
    """
    Parse a transcript and execute the HA action.

    Args:
        transcript: Spoken text from STT
        bridge: HABridge or HABridgeStub instance
        stub: If True, uses SAMPLE_ENTITIES (no live HA)
        scheduler: Optional SceneScheduler for voice trigger handling

    Returns:
        Result dict with action taken and HA response
    """
    logger.info(f"Parsing intent: {transcript!r}")

    # Check for scene voice triggers first (before intent parser)
    if scheduler:
        scene_result = _handle_scene_trigger(transcript, scheduler, bridge)
        if scene_result is not None:
            return scene_result

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

    # Handle conversational (non-HA) queries via OpenClaw fallback.
    # Lists are always multi-action HA commands (e.g. "turn off lights and lock door").
    # Dicts with action_type not in HA actions are conversational queries.
    if isinstance(action, list):
        action_type = "call_service"  # multi-action list always goes to HA
    else:
        action_type = action.get("action") if isinstance(action, dict) else None

    _HA_ACTION_TYPES = {"call_service", "query", "clarify"}
    if action_type not in _HA_ACTION_TYPES:
        logger.info(f"Action type {action_type!r} is non-HA — routing to conversational fallback")
        response_text = _conversational_fallback(transcript)
        return {
            "transcript": transcript,
            "action": action,
            "results": [],
            "response": response_text,
            "fallback": True,
        }

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


# ---------------------------------------------------------------------------
# Conversational fallback (non-HA queries → OpenClaw)
# ---------------------------------------------------------------------------

def _conversational_fallback(transcript: str) -> str:
    """
    Route a non-device-control query to OpenClaw's local chat completions endpoint.
    Falls back to a static keyword-matched response if gateway is unavailable.
    """
    try:
        from conversation_fallback import ConversationFallback
        fb = ConversationFallback(timeout=10.0)
        return fb.respond(transcript)
    except ImportError:
        logger.warning("conversation_fallback not found — using generic response")
        return "I didn't catch that as a home command. Could you try again?"
    except Exception as e:
        logger.warning(f"Conversational fallback error: {e}")
        return "Sorry, I couldn't process that right now."


# ---------------------------------------------------------------------------
# Response formatting helpers
# ---------------------------------------------------------------------------

def domain_from_entity(entity_id: str) -> str:
    """Return the domain part of an entity_id (e.g. 'sensor.temp' → 'sensor')."""
    return entity_id.split(".")[0] if "." in entity_id else entity_id


def _friendly_entity(entity_id: str) -> str:
    """Turn entity_id into readable name: 'light.living_room_ceiling' → 'living room ceiling light'."""
    parts = entity_id.split(".", 1)
    domain = parts[0] if len(parts) > 1 else ""
    name = parts[1] if len(parts) > 1 else entity_id
    readable = name.replace("_", " ")
    # Append domain hint for sensors/locks
    if domain in ("sensor", "binary_sensor"):
        return readable
    if domain == "lock":
        return f"{readable} lock"
    if domain == "light":
        return f"{readable} light"
    if domain == "switch":
        return f"{readable} switch"
    return readable


def _room_from_entity(entity_id: str) -> str:
    """Extract room name from entity_id: 'sensor.living_room_temperature' → 'living room'."""
    _ROOM_KEYWORDS = [
        "living_room", "kitchen", "bedroom", "hallway", "entrance",
        "dining_room", "bathroom", "garden", "courtyard", "office",
    ]
    lower = entity_id.lower()
    for room in _ROOM_KEYWORDS:
        if room in lower:
            return room.replace("_", " ")
    # Fallback: take the first segment after the domain
    parts = entity_id.split(".", 1)
    if len(parts) > 1:
        segments = parts[1].split("_")
        # Usually room is the first word(s) before the sensor type
        if len(segments) >= 2:
            return " ".join(segments[:-1])
    return "the room"


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

    if action_type == "clear_override":
        return "Welcome home! Auto schedule resumed."

    elif action_type == "activate_scene":
        return build_scene_response(action.get("scene", "unknown"), results)

    elif action_type == "clarify":
        return action.get("question", "Could you clarify?")

    elif action_type == "query":
        # Build a natural-language response for sensor/state queries
        if results and results[0].get("ok"):
            state = results[0].get("state", "unknown")
            entity_id = action.get("entity_id", "")
            friendly_name = results[0].get("attributes", {}).get("friendly_name", "")

            # Skip stub placeholder — just report unknown
            if state == "stub":
                name = friendly_name or _friendly_entity(entity_id)
                return f"I don't have a live reading for {name} yet."

            # Sensor-specific natural phrasing
            device_class = results[0].get("attributes", {}).get("device_class", "")
            unit = results[0].get("attributes", {}).get("unit_of_measurement", "")
            name = friendly_name or _friendly_entity(entity_id)

            if device_class == "temperature" or "temperature" in entity_id:
                return f"It's {state}{unit} in the {_room_from_entity(entity_id)}."
            elif device_class == "humidity" or "humidity" in entity_id:
                return f"Humidity in the {_room_from_entity(entity_id)} is {state}{unit}."
            elif device_class in ("door", "window", "motion"):
                open_closed = "open" if state == "on" else "closed"
                return f"The {name} is {open_closed}."
            elif domain_from_entity(entity_id) == "lock":
                return f"The {name} is {state}."
            elif unit:
                return f"The {name} is {state} {unit}."
            else:
                return f"The {name} is {state}."
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


def build_scene_response(scene_name: str, results: list) -> str:
    """Build a natural-language TTS response for scene activation."""
    failures = [r for r in results if not r.get("ok", True)]
    success_count = len(results) - len(failures)

    scene_phrases = {
        "morning": "Good morning! Lights are on and the day is starting.",
        "night": "Goodnight. Hallway light is on, everything else is off.",
        "daytime": "Daytime mode — lights are off to save energy.",
        "evening": "Evening scene set — living room and kitchen are cosy.",
        "away": "I've secured the house. All lights off, climate off.",
    }

    if failures and success_count == 0:
        return f"I tried to set {scene_name} but nothing responded. Check the hardware."

    base = scene_phrases.get(scene_name, f"{scene_name.title()} scene activated.")

    if failures:
        base += f" ({success_count} actions done, {len(failures)} failed)"

    return base


# ---------------------------------------------------------------------------
# Wake word event loop (reads stdout from wake_word_bridge.py)
# ---------------------------------------------------------------------------

def run_pipeline_from_stdin(stub: bool = False, ha_events: bool = False, scene_scheduler: bool = False):
    """
    Main pipeline loop: reads JSON events from stdin (piped from wake_word_bridge.py).
    On wake_word_detected: records audio → STT → intent → HA.
    Also processes HA state_changed events for proactive alerts.

    Usage:
      python3 wake_word/wake_word_bridge.py | python3 pipeline.py

      # With HA event emitter thread (feeds state_changed events into pipeline):
      python3 wake_word/wake_word_bridge.py | python3 pipeline.py --ha-events

      # With scene scheduler (auto-evaluates time-of-day scenes):
      python3 wake_word/wake_word_bridge.py | python3 pipeline.py --scene-scheduler

    In stub mode:
      echo '{"type":"wake_word_detected","word":"claudette","backend":"stub"}' | python3 pipeline.py --stub
    """
    from ha_bridge import get_bridge
    bridge = get_bridge(stub=stub)

    # Initialize proactive alert pipeline integration
    alert_integration = _init_alert_integration()

    # Initialize scene scheduler if requested
    scheduler = _init_scene_scheduler() if scene_scheduler else None

    # Start HA event emitter thread if requested
    ha_emitter = None
    if ha_events and not stub:
        ha_emitter = _init_ha_event_emitter(alert_integration)

    logger.info(f"Pipeline started. Waiting for wake word events... (stub={stub}, ha_events={ha_events}, scene_scheduler={scene_scheduler})")

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
            # Evaluate time-based scenes at conversation start
            if scheduler:
                try:
                    active = scheduler.evaluate()
                    if active:
                        logger.info(f"Time-based scene eval: {active}")
                except Exception as e:
                    logger.warning(f"Scene eval failed: {e}")
            # Deliver any batched low-priority alerts at conversation start
            if alert_integration:
                delivered = alert_integration.on_conversation_start()
                if delivered:
                    logger.info(f"Delivered {delivered} batched alert(s) at conversation start")
            _handle_wake_word(bridge, stub, scheduler)

        elif event_type == "state_changed":
            # HA WebSocket state change → feed to proactive alert engine
            if alert_integration:
                modes = alert_integration.on_ha_event(line)
                if modes:
                    logger.info(f"Alert routing results: {modes}")

        elif event_type == "listener_started":
            logger.info(f"Wake word listener started: {event}")

        elif event_type == "listener_stopped":
            logger.info("Wake word listener stopped")
            break

        elif event_type == "error":
            logger.error(f"Wake word error: {event}")


def _init_ha_event_emitter(alert_integration):
    """
    Start the HA WebSocket event emitter in a background thread.
    Routes state_changed events through the alert integration.
    Returns the emitter thread, or None if unavailable.
    """
    try:
        from ha_event_emitter import HAEventEmitterThread

        def on_ha_event(event):
            """Callback from HA emitter thread → route through alert engine."""
            if alert_integration and event.get("type") == "state_changed":
                import json as _json
                modes = alert_integration.on_ha_event(_json.dumps(event))
                if modes:
                    logger.info(f"HA event alert routing: {modes}")

        emitter = HAEventEmitterThread(callback=on_ha_event)
        emitter.start()
        logger.info("HA event emitter thread started — proactive alerts are live")
        return emitter
    except Exception as e:
        logger.warning(f"HA event emitter unavailable: {e}")
        return None


def _init_alert_integration():
    """
    Initialize the proactive alert pipeline integration.
    Returns AlertPipelineIntegration or None if import fails.
    """
    try:
        from brain.alert_delivery import AlertPipelineIntegration
        integration = AlertPipelineIntegration()
        logger.info("Proactive alert integration initialized")
        return integration
    except Exception as e:
        logger.warning(f"Proactive alert integration unavailable: {e}")
        return None


def _handle_wake_word(bridge, stub: bool, scheduler=None):
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
    result = handle_transcript(transcript, bridge, stub=stub, scheduler=scheduler)
    response = result.get("response", "")
    if response:
        logger.info(f"Response: {response}")
        # TODO: TTS — pipe response to sag/tts or OpenClaw's tts endpoint
        # For now, just print — the systemd unit can pipe to tts separately
        print(json.dumps({"type": "pipeline_response", "text": response}), flush=True)


# ---------------------------------------------------------------------------
# Text mode — bypass wake word + STT, type a command
# ---------------------------------------------------------------------------

def run_text_mode(text: str, stub: bool = False, scene_scheduler: bool = False):
    """
    Shortcut: skip wake word and STT, directly parse a typed command.
    Good for testing intent parser + HA bridge from the CLI.
    """
    from ha_bridge import get_bridge
    bridge = get_bridge(stub=stub)
    scheduler = _init_scene_scheduler() if scene_scheduler else None

    result = handle_transcript(text, bridge, stub=stub, scheduler=scheduler)
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
ExecStart=/bin/bash -c 'python3 wake_word/wake_word_bridge.py | python3 pipeline.py --ha-events'
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
        "--ha-events", action="store_true",
        help="Start HA WebSocket event emitter thread for proactive alerts"
    )
    parser.add_argument(
        "--scene-scheduler", action="store_true",
        help="Enable scene scheduler (time-of-day eval + voice trigger routing)"
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
        run_text_mode(args.text, stub=args.stub, scene_scheduler=args.scene_scheduler)
    else:
        # Read wake word events from stdin (piped from wake_word_bridge.py)
        run_pipeline_from_stdin(stub=args.stub, ha_events=args.ha_events, scene_scheduler=args.scene_scheduler)


if __name__ == "__main__":
    main()
