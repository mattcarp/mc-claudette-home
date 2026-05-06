#!/usr/bin/env python3
"""
Tests for voice/wake_word/wake_word_bridge.py

No audio hardware required.
Focuses on deterministic stub mode and event contract.
"""

import io
import json
import os
import subprocess
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str((Path(__file__).parent / "wake_word").resolve()))

from wake_word_bridge import _post_audio_to_stt

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


def _make_wav_bytes(duration_s: float = 0.25, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(sample_rate * duration_s))
    return buf.getvalue()


class _CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        payload = json.dumps(self.server.response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class _CapturedServer:
    def __init__(self, response_payload=None):
        self.httpd = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.httpd.response_payload = response_payload or {
            "text": "turn on the kitchen light",
            "language": "en",
            "duration_ms": 42,
            "backend": "stub",
        }
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    @property
    def captured(self):
        return getattr(self.httpd, "captured", None)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


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


def test_post_audio_to_stt_uses_multipart_upload_contract():
    wav_bytes = _make_wav_bytes()

    with _CapturedServer() as server:
        response = _post_audio_to_stt(server.url, wav_bytes)

    assert response["text"] == "turn on the kitchen light"
    captured = server.captured
    assert captured is not None
    assert captured["path"] == "/transcribe"
    assert "multipart/form-data" in captured["headers"]["Content-Type"]
    assert b'Content-Disposition: form-data; name="audio"; filename="command.wav"' in captured["body"]
    assert b"Content-Type: audio/wav" in captured["body"]
    assert b"RIFF" in captured["body"]


def test_post_audio_to_stt_forwards_bearer_token_when_configured():
    wav_bytes = _make_wav_bytes()
    original = os.environ.get("STT_API_KEY")
    os.environ["STT_API_KEY"] = "test-secret"

    try:
        with _CapturedServer() as server:
            _post_audio_to_stt(server.url, wav_bytes)
        assert server.captured is not None
        assert server.captured["headers"]["Authorization"] == "Bearer test-secret"
    finally:
        if original is None:
            os.environ.pop("STT_API_KEY", None)
        else:
            os.environ["STT_API_KEY"] = original
