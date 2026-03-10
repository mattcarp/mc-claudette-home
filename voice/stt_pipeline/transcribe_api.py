#!/usr/bin/env python3
"""
Claudette Home — STT Pipeline API
Workshop-side FastAPI endpoint for audio transcription via Whisper.

Architecture:
  Android panel (mic) → wake word detected
    → record audio → POST /transcribe
    → Whisper transcribes
    → { "text": "turn on the living room lights" }
    → OpenClaw / intent parser handles the rest

Usage:
  pip install -r requirements.txt
  uvicorn transcribe_api:app --host 0.0.0.0 --port 8765

Environment:
  WHISPER_MODEL     — model size (default: base.en — fast, English only)
                      Options: tiny.en, base.en, small.en, medium, large-v3
                      Note: openai-whisper uses 'base' not 'base.en' — auto-mapped
  WHISPER_LANGUAGE  — language hint (default: en)
                      For Malta: consider "en" — Maltese/Italian handled by larger models
  STT_API_KEY       — optional auth token (if set, clients must send Bearer <token>)
  STT_PORT          — port to bind (default: 8765)
  STT_HOST          — host to bind (default: 0.0.0.0)

Backend selection (auto-detected):
  1. faster-whisper — preferred (lower latency, int8 quantisation)
  2. openai-whisper — fallback (standard, works on Workshop today)
  3. stub           — no Whisper at all (returns mock transcript)
"""

import io
import logging
import os
import time
from typing import Optional

try:
    from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
    from fastapi.responses import JSONResponse
except ImportError:
    raise ImportError("fastapi not installed — run: pip install fastapi uvicorn")

# ---------------------------------------------------------------------------
# Whisper backend detection — prefer faster-whisper, fall back to openai-whisper
# ---------------------------------------------------------------------------
WHISPER_BACKEND = "stub"

try:
    from faster_whisper import WhisperModel as FasterWhisperModel
    WHISPER_BACKEND = "faster-whisper"
except ImportError:
    pass

if WHISPER_BACKEND == "stub":
    try:
        import whisper as openai_whisper  # openai-whisper
        WHISPER_BACKEND = "openai-whisper"
    except ImportError:
        pass  # stays as stub

WHISPER_AVAILABLE = WHISPER_BACKEND != "stub"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logger.info(f"Whisper backend: {WHISPER_BACKEND}")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "base.en")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")
API_KEY = os.environ.get("STT_API_KEY")  # Optional auth
STUB_MODE = not WHISPER_AVAILABLE

# openai-whisper uses model names without the language suffix (base.en → base)
# Map model names for compatibility
_OAI_MODEL_MAP = {
    "tiny.en": "tiny.en",
    "base.en": "base.en",
    "small.en": "small.en",
    "medium.en": "medium.en",
    "tiny": "tiny",
    "base": "base",
    "small": "small",
    "medium": "medium",
    "large-v3": "large",
    "large-v2": "large",
    "large": "large",
}
OAI_MODEL_SIZE = _OAI_MODEL_MAP.get(MODEL_SIZE, MODEL_SIZE)

app = FastAPI(
    title="Claudette Home — STT Pipeline",
    description="Local Whisper transcription endpoint for Claudette voice pipeline",
    version="0.2.0",
)

# Lazy-load model on first request (faster startup)
_model = None


def get_model():
    """Load and cache the Whisper model on first use."""
    global _model
    if _model is not None or not WHISPER_AVAILABLE:
        return _model

    logger.info(f"Loading Whisper model ({WHISPER_BACKEND}): {MODEL_SIZE}")
    t0 = time.time()

    if WHISPER_BACKEND == "faster-whisper":
        _model = FasterWhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    elif WHISPER_BACKEND == "openai-whisper":
        _model = openai_whisper.load_model(OAI_MODEL_SIZE)

    logger.info(f"Whisper model loaded in {time.time() - t0:.1f}s")
    return _model


# ---------------------------------------------------------------------------
# Auth (optional)
# ---------------------------------------------------------------------------
security = HTTPBearer(auto_error=False)


def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not API_KEY:
        return  # Auth disabled
    if credentials is None or credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Health check — returns model status and mode."""
    return {
        "status": "ok",
        "mode": "stub" if STUB_MODE else "live",
        "backend": WHISPER_BACKEND,
        "model": MODEL_SIZE if not STUB_MODE else None,
        "whisper_loaded": _model is not None,
    }


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(..., description="Audio file (WAV, 16kHz mono preferred)"),
    _: None = Depends(verify_token),
):
    """
    Transcribe audio to text.

    Accepts: audio/wav, audio/webm, audio/ogg, audio/mp4, audio/mpeg
    Returns: {"text": "...", "language": "en", "duration_ms": 420, "model": "base.en"}

    The panel should:
    1. Record 16kHz mono PCM WAV from mic
    2. POST it here as multipart/form-data with field name 'audio'
    3. Use the returned text as the transcript for the intent parser
    """
    t0 = time.time()
    audio_bytes = await audio.read()

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    logger.info(f"Received audio: {len(audio_bytes)} bytes, type={audio.content_type}")

    if STUB_MODE:
        # Development stub — no Whisper installed
        # Useful for testing the pipeline end-to-end without GPU/Whisper
        logger.warning("STUB MODE: Whisper not installed — returning mock transcript")
        return JSONResponse({
            "text": "stub transcript — install faster-whisper or openai-whisper",
            "language": "en",
            "duration_ms": int((time.time() - t0) * 1000),
            "model": "stub",
            "backend": "stub",
            "stub": True,
        })

    model = get_model()
    audio_io = io.BytesIO(audio_bytes)

    if WHISPER_BACKEND == "faster-whisper":
        # faster-whisper: streaming segments API
        segments, info = model.transcribe(
            audio_io,
            language=LANGUAGE if LANGUAGE else None,
            beam_size=5,
            vad_filter=True,  # Skip silence — faster for home command style audio
            vad_parameters=dict(min_silence_duration_ms=300),
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        detected_language = info.language

    elif WHISPER_BACKEND == "openai-whisper":
        # openai-whisper: needs a temp file (doesn't accept BytesIO for some models)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            result = model.transcribe(
                tmp_path,
                language=LANGUAGE if LANGUAGE else None,
                fp16=False,  # CPU — no fp16
            )
            text = result.get("text", "").strip()
            detected_language = result.get("language", LANGUAGE)
        finally:
            import os as _os
            _os.unlink(tmp_path)

    else:
        raise RuntimeError(f"Unknown backend: {WHISPER_BACKEND}")

    duration_ms = int((time.time() - t0) * 1000)
    logger.info(f"Transcribed [{WHISPER_BACKEND}] in {duration_ms}ms: {text!r}")

    return JSONResponse({
        "text": text,
        "language": detected_language,
        "duration_ms": duration_ms,
        "model": MODEL_SIZE,
        "backend": WHISPER_BACKEND,
    })


@app.get("/models")
def list_models():
    """Available Whisper models with latency/accuracy tradeoffs for reference."""
    return {
        "models": [
            {"name": "tiny.en",   "size_mb": 75,   "wer_pct": 5.9, "speed": "fastest", "note": "good enough for clear commands"},
            {"name": "base.en",   "size_mb": 145,  "wer_pct": 4.2, "speed": "fast",    "note": "recommended default"},
            {"name": "small.en",  "size_mb": 466,  "wer_pct": 3.4, "speed": "medium",  "note": "better with accents"},
            {"name": "medium",    "size_mb": 1500, "wer_pct": 3.0, "speed": "slow",    "note": "multilingual (Maltese/Italian)"},
            {"name": "large-v3",  "size_mb": 3100, "wer_pct": 2.7, "speed": "slowest", "note": "best quality, shared with Kenneth"},
        ],
        "current": MODEL_SIZE,
        "backend": WHISPER_BACKEND,
        "note": "For Malta, 'medium' handles Maltese-accented English and Italian. 'base.en' fine for plain English. faster-whisper preferred; openai-whisper works as fallback on Workshop CPU."
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("STT_HOST", "0.0.0.0")
    port = int(os.environ.get("STT_PORT", "8765"))
    logger.info(f"Starting STT API on {host}:{port} (model={MODEL_SIZE}, stub={STUB_MODE})")
    uvicorn.run(app, host=host, port=port, log_level="info")
