#!/usr/bin/env python3
"""
Claudette Home — Wake Word Bridge
Unified interface that wraps either Porcupine or openWakeWord.
Swap the backend without touching the rest of the pipeline.

Usage:
  python3 wake_word_bridge.py --backend porcupine --model models/claudette_linux.ppn
  python3 wake_word_bridge.py --backend porcupine --builtin-keyword porcupine
  python3 wake_word_bridge.py --backend oww --model models/claudette.tflite
  python3 wake_word_bridge.py --backend stub --max-events 3

Environment:
  WAKE_WORD_BACKEND=porcupine|oww|stub (default: porcupine)
  PORCUPINE_ACCESS_KEY=...             (required for porcupine backend)
  WAKE_WORD_MODEL=path/to/model        (optional, overrides --model default)
  WAKE_WORD_BUILTIN_KEYWORD=keyword    (optional, Porcupine built-in keyword)
  WAKE_WORD_THRESHOLD=0.5              (optional, detection threshold)

This script is the entry point for the systemd service (future).
On detection: writes event to stdout (JSON) and triggers callback.
Other services (STT, intent parser) can listen to that stream.
"""

import argparse
import json
import os
import signal
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Optional


def emit_event(event_type: str, data: dict):
    """Write a JSON event line to stdout. Downstream services can tail this."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        **data,
    }
    print(json.dumps(event), flush=True)


def on_detection(backend: str, word: str, score: float = None, stt_url: Optional[str] = None):
    """Called on wake word detection. Emits event and optionally triggers STT pipeline."""
    emit_event("wake_word_detected", {
        "backend": backend,
        "word": word,
        "score": score,
    })

    # Trigger STT pipeline (issue #10): VAD record → POST to STT API
    if stt_url:
        try:
            _run_stt_pipeline(stt_url)
        except Exception as e:
            emit_event("stt_error", {"error": str(e)})
    else:
        emit_event("stt_skipped", {"reason": "no STT URL configured"})


def _run_stt_pipeline(stt_url: str):
    """Record speech with VAD, send to STT API, emit transcript event."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from vad_recorder import create_vad_recorder, VadConfig

    emit_event("stt_recording_start", {})
    recorder = create_vad_recorder(VadConfig(silence_timeout=1.5, max_duration=10.0))
    result = recorder.record()
    emit_event("stt_recording_done", {
        "speech_detected": result.speech_detected,
        "speech_duration_ms": result.speech_duration_ms,
        "audio_bytes": len(result.audio_bytes),
    })

    if not result.speech_detected:
        emit_event("stt_no_speech", {})
        return

    # POST audio to STT API
    import urllib.request
    req = urllib.request.Request(
        f"{stt_url}/transcribe",
        data=result.audio_bytes,
        headers={"Content-Type": "audio/wav"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())

    transcript = body.get("text", "")
    emit_event("stt_transcript", {
        "text": transcript,
        "language": body.get("language"),
        "duration_ms": body.get("duration_ms"),
        "backend": body.get("backend"),
    })
    # TODO (issue #7): Pass transcript to intent parser


def run_porcupine(
    model_path: Optional[str],
    access_key: str,
    sensitivity: float,
    stt_url: Optional[str] = None,
    builtin_keyword: Optional[str] = None,
):
    """Run Porcupine backend with either a custom .ppn model or a built-in keyword."""
    import pvporcupine
    import pyaudio

    create_kwargs = {
        "access_key": access_key,
        "sensitivities": [sensitivity],
    }
    if builtin_keyword:
        create_kwargs["keywords"] = [builtin_keyword]
    else:
        create_kwargs["keyword_paths"] = [model_path]

    porcupine = pvporcupine.create(**create_kwargs)
    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length,
    )

    emit_event("listener_started", {
        "backend": "porcupine",
        "model": model_path,
        "builtin_keyword": builtin_keyword,
        "sensitivity": sensitivity,
        "sample_rate": porcupine.sample_rate,
    })

    def shutdown(sig, frame):
        stream.close()
        pa.terminate()
        porcupine.delete()
        emit_event("listener_stopped", {"backend": "porcupine"})
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        pcm = stream.read(porcupine.frame_length, exception_on_overflow=False)
        pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)
        if porcupine.process(pcm) >= 0:
            on_detection(
                "porcupine",
                builtin_keyword or "claudette",
                stt_url=stt_url,
            )


def run_oww(model_path: str, threshold: float, stt_url: Optional[str] = None):
    """Run openWakeWord backend."""
    import numpy as np
    import pyaudio
    from openwakeword.model import Model

    oww_model = Model(wakeword_models=[model_path])
    chunk = 1620
    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=16000,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=chunk,
    )

    emit_event("listener_started", {
        "backend": "oww",
        "model": model_path,
        "threshold": threshold,
    })

    def shutdown(sig, frame):
        stream.close()
        pa.terminate()
        emit_event("listener_stopped", {"backend": "oww"})
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        audio = stream.read(chunk, exception_on_overflow=False)
        audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        for word, score in oww_model.predict(audio_np).items():
            if score >= threshold:
                on_detection("oww", word, score, stt_url=stt_url)


def run_stub(
    max_events: int = 1,
    interval: float = 0.1,
    word: str = "claudette",
    stt_url: Optional[str] = None,
):
    """
    Deterministic fake backend for CI/dev runs with no audio hardware.
    Emits listener lifecycle events plus a fixed number of wake detections.
    """
    emit_event("listener_started", {
        "backend": "stub",
        "word": word,
        "max_events": max_events,
        "interval": interval,
    })

    count = 0
    try:
        while max_events <= 0 or count < max_events:
            count += 1
            on_detection("stub", word, score=1.0, stt_url=stt_url)
            if interval > 0:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        emit_event("listener_stopped", {"backend": "stub", "emitted": count})


def main():
    parser = argparse.ArgumentParser(description="Claudette Home — Wake Word Bridge")
    parser.add_argument(
        "--backend",
        default=os.environ.get("WAKE_WORD_BACKEND", "porcupine"),
        choices=["porcupine", "oww", "stub"],
        help="Wake word backend (default: porcupine)"
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("WAKE_WORD_MODEL"),
        help="Path to model file (.ppn for porcupine, .tflite for oww)"
    )
    parser.add_argument(
        "--builtin-keyword",
        default=os.environ.get("WAKE_WORD_BUILTIN_KEYWORD"),
        help="Porcupine built-in keyword to use instead of a custom .ppn model"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("WAKE_WORD_THRESHOLD", "0.5")),
        help="Detection threshold (default: 0.5)"
    )
    parser.add_argument(
        "--access-key",
        default=os.environ.get("PORCUPINE_ACCESS_KEY"),
        help="Picovoice access key (porcupine backend only)"
    )
    parser.add_argument(
        "--stt-url",
        default=os.environ.get("STT_API_URL", ""),
        help="STT API URL (e.g. http://127.0.0.1:8765). If set, triggers VAD+STT on wake word.",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=int(os.environ.get("WAKE_WORD_MAX_EVENTS", "1")),
        help="Stub backend only: number of wake events to emit (<=0 means forever).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("WAKE_WORD_STUB_INTERVAL", "0.1")),
        help="Stub backend only: seconds between emitted wake events.",
    )
    args = parser.parse_args()

    base = os.path.dirname(__file__)
    stt_url = args.stt_url or None

    if args.backend == "stub":
        run_stub(
            max_events=args.max_events,
            interval=args.interval,
            word=args.builtin_keyword or "claudette",
            stt_url=stt_url,
        )
        return

    if args.backend == "porcupine":
        if args.builtin_keyword and args.model:
            print('{"error": "Use either --model or --builtin-keyword, not both"}', flush=True)
            sys.exit(1)
        if not args.access_key:
            print('{"error": "PORCUPINE_ACCESS_KEY not set"}', flush=True)
            sys.exit(1)
        if not args.builtin_keyword and not args.model:
            args.model = os.path.join(base, "models", "claudette_linux.ppn")
        if not args.builtin_keyword and not os.path.exists(args.model):
            print(json.dumps({"error": f"Porcupine model not found: {args.model}"}), flush=True)
            sys.exit(1)
        run_porcupine(
            args.model,
            args.access_key,
            args.threshold,
            stt_url=stt_url,
            builtin_keyword=args.builtin_keyword,
        )
    else:
        if not args.model:
            args.model = os.path.join(base, "models", "claudette.tflite")
        if not os.path.exists(args.model):
            print(json.dumps({"error": f"openWakeWord model not found: {args.model}"}), flush=True)
            sys.exit(1)
        run_oww(args.model, args.threshold, stt_url=stt_url)


if __name__ == "__main__":
    main()
