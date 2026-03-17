#!/usr/bin/env python3
"""
Claudette Home — STT Pipeline API Tests
Comprehensive tests for transcribe_api.py (FastAPI Whisper endpoint).

Tests the API at multiple levels:
  1. Health endpoint — works without Whisper loaded
  2. Model listing — /models metadata
  3. Transcription — stub mode, openai-whisper, and faster-whisper
  4. Auth enforcement — STT_API_KEY gating
  5. Error handling — empty audio, missing file, bad format
  6. WAV generation — valid PCM WAV for testing

Run:
  python3 -m pytest voice/stt_pipeline/test_transcribe_api.py -v
  # Or from the stt_pipeline dir:
  python3 -m pytest test_transcribe_api.py -v

No live mic or GPU required. Uses synthesised WAV bytes for all audio tests.
"""

import io
import os
import struct
import sys
import wave

import pytest

# Ensure the stt_pipeline dir is on the path
STT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STT_DIR)

# ---------------------------------------------------------------------------
# Import the FastAPI app for TestClient use
# ---------------------------------------------------------------------------
from transcribe_api import app, WHISPER_BACKEND, WHISPER_AVAILABLE, STUB_MODE

try:
    from starlette.testclient import TestClient
except ImportError:
    from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers — generate valid WAV audio bytes
# ---------------------------------------------------------------------------

def make_wav_bytes(
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    frequency: float = 440.0,
    amplitude: int = 3000,
) -> bytes:
    """
    Generate a valid WAV file (16-bit mono PCM) containing a sine tone.
    Good enough for Whisper to accept and process (even if transcript is empty).
    """
    import math

    num_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(amplitude * math.sin(2.0 * math.pi * frequency * t))
        samples.append(struct.pack("<h", max(-32768, min(32767, value))))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(samples))
    return buf.getvalue()


def make_silent_wav(duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Generate a silent WAV file — useful for testing Whisper returns empty text."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(sample_rate * duration_s))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """TestClient for the FastAPI app — no auth."""
    return TestClient(app)


@pytest.fixture
def wav_bytes():
    """1-second 440Hz sine wave WAV."""
    return make_wav_bytes(duration_s=1.0)


@pytest.fixture
def silent_wav():
    """0.5-second silent WAV."""
    return make_silent_wav(duration_s=0.5)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_fields(self, client):
        data = client.get("/health").json()
        assert "status" in data
        assert "mode" in data
        assert "backend" in data
        assert data["status"] == "ok"

    def test_health_backend_matches_module(self, client):
        data = client.get("/health").json()
        assert data["backend"] == WHISPER_BACKEND

    def test_health_mode_matches_availability(self, client):
        data = client.get("/health").json()
        if WHISPER_AVAILABLE:
            assert data["mode"] == "live"
        else:
            assert data["mode"] == "stub"


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------

class TestModels:
    def test_models_returns_200(self, client):
        r = client.get("/models")
        assert r.status_code == 200

    def test_models_has_model_list(self, client):
        data = client.get("/models").json()
        assert "models" in data
        assert isinstance(data["models"], list)
        assert len(data["models"]) >= 5

    def test_models_have_required_fields(self, client):
        data = client.get("/models").json()
        for model in data["models"]:
            assert "name" in model
            assert "size_mb" in model
            assert "speed" in model

    def test_models_current_field(self, client):
        data = client.get("/models").json()
        assert "current" in data
        assert data["current"] is not None

    def test_models_includes_base_en(self, client):
        data = client.get("/models").json()
        names = [m["name"] for m in data["models"]]
        assert "base.en" in names


# ---------------------------------------------------------------------------
# Transcription — core tests
# ---------------------------------------------------------------------------

class TestTranscribe:
    """Tests that work regardless of which Whisper backend is installed."""

    def test_transcribe_returns_200(self, client, wav_bytes):
        r = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        )
        assert r.status_code == 200

    def test_transcribe_response_structure(self, client, wav_bytes):
        data = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        ).json()
        assert "text" in data
        assert "language" in data
        assert "duration_ms" in data
        assert "model" in data
        assert "backend" in data

    def test_transcribe_duration_positive(self, client, wav_bytes):
        data = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        ).json()
        assert data["duration_ms"] >= 0

    def test_transcribe_backend_matches(self, client, wav_bytes):
        data = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        ).json()
        assert data["backend"] == WHISPER_BACKEND

    def test_transcribe_text_is_string(self, client, wav_bytes):
        data = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        ).json()
        assert isinstance(data["text"], str)

    def test_transcribe_silent_wav(self, client, silent_wav):
        """Silent audio should return a string (possibly empty) — no crash."""
        data = client.post(
            "/transcribe",
            files={"audio": ("silence.wav", io.BytesIO(silent_wav), "audio/wav")},
        ).json()
        assert isinstance(data["text"], str)


# ---------------------------------------------------------------------------
# Transcription — backend-specific
# ---------------------------------------------------------------------------

class TestTranscribeWhisper:
    """Tests that only make sense when a real Whisper backend is available."""

    @pytest.mark.skipif(not WHISPER_AVAILABLE, reason="No Whisper backend installed")
    def test_transcribe_live_returns_text(self, client, wav_bytes):
        data = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        ).json()
        # Live Whisper should not have stub=True
        assert data.get("stub") is not True
        assert data["model"] != "stub"

    @pytest.mark.skipif(WHISPER_AVAILABLE, reason="Whisper IS available — testing stub path")
    def test_stub_mode_returns_stub_transcript(self, client, wav_bytes):
        data = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        ).json()
        assert data.get("stub") is True
        assert data["backend"] == "stub"
        assert "stub" in data["text"].lower()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_transcribe_no_file_returns_422(self, client):
        """POST /transcribe without the 'audio' field → 422 Unprocessable Entity."""
        r = client.post("/transcribe")
        assert r.status_code == 422

    def test_transcribe_empty_audio_returns_400(self, client):
        """POST with an empty audio file → 400."""
        r = client.post(
            "/transcribe",
            files={"audio": ("empty.wav", io.BytesIO(b""), "audio/wav")},
        )
        assert r.status_code == 400

    def test_health_post_not_allowed(self, client):
        """GET-only endpoint shouldn't accept POST."""
        r = client.post("/health")
        assert r.status_code == 405

    def test_nonexistent_route_returns_404(self, client):
        r = client.get("/does-not-exist")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Auth tests (only relevant if STT_API_KEY is set)
# ---------------------------------------------------------------------------

class TestAuth:
    @pytest.mark.skipif(
        not os.environ.get("STT_API_KEY"),
        reason="STT_API_KEY not set — auth disabled",
    )
    def test_auth_required_returns_401(self, client, wav_bytes):
        """Without Bearer token, should get 401."""
        r = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
        )
        assert r.status_code == 401

    @pytest.mark.skipif(
        not os.environ.get("STT_API_KEY"),
        reason="STT_API_KEY not set — auth disabled",
    )
    def test_auth_wrong_key_returns_401(self, client, wav_bytes):
        r = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
            headers={"Authorization": "Bearer wrong-key-12345"},
        )
        assert r.status_code == 401

    @pytest.mark.skipif(
        not os.environ.get("STT_API_KEY"),
        reason="STT_API_KEY not set — auth disabled",
    )
    def test_auth_valid_key_returns_200(self, client, wav_bytes):
        key = os.environ["STT_API_KEY"]
        r = client.post(
            "/transcribe",
            files={"audio": ("test.wav", io.BytesIO(wav_bytes), "audio/wav")},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# WAV helper tests
# ---------------------------------------------------------------------------

class TestWAVHelpers:
    def test_make_wav_bytes_valid(self):
        wav = make_wav_bytes(duration_s=0.5)
        buf = io.BytesIO(wav)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getsampwidth() == 2
            frames = wf.readframes(wf.getnframes())
            # 0.5s * 16000 * 2 bytes = 16000 bytes of PCM data
            assert len(frames) == 16000

    def test_make_silent_wav_valid(self):
        wav = make_silent_wav(duration_s=1.0)
        buf = io.BytesIO(wav)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 16000

    def test_wav_header_magic(self):
        wav = make_wav_bytes(duration_s=0.1)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"


# ---------------------------------------------------------------------------
# API metadata
# ---------------------------------------------------------------------------

class TestAPIMetadata:
    def test_openapi_schema_available(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["info"]["title"] == "Claudette Home — STT Pipeline"
        assert "/health" in schema["paths"]
        assert "/transcribe" in schema["paths"]
        assert "/models" in schema["paths"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
