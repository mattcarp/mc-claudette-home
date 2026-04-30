# STT Pipeline — Whisper Transcription API

Workshop-side FastAPI server. The Android panel sends audio here after wake word detection, gets back text, which goes to the intent parser.

## Architecture

```
Android Panel
  │
  │  POST /transcribe  (audio/wav, 16kHz mono)
  ▼
Workshop STT API (port 8765)
  │
  │  faster-whisper runs locally, no cloud
  ▼
{ "text": "turn on the living room lights" }
  │
  ▼
Intent Parser (issue #7) → HA Bridge (issue #6) → Home Assistant
```

## Quick Start

```bash
pip install -r requirements.txt

# Development (stub mode — no Whisper needed)
python3 transcribe_api.py

# Production
WHISPER_MODEL=base.en python3 transcribe_api.py
```

## Install as systemd service (Workshop)

```bash
sudo cp claudette-stt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable claudette-stt
sudo systemctl start claudette-stt
sudo journalctl -u claudette-stt -f
```

## API

### POST /transcribe
Upload audio file for transcription.

```bash
curl -X POST http://workshop:8765/transcribe \
  -F "audio=@recording.wav"

# Response:
{
  "text": "turn on the living room lights",
  "language": "en",
  "duration_ms": 387,
  "model": "base.en"
}
```

### GET /health
```json
{"status": "ok", "mode": "live", "model": "base.en", "whisper_loaded": true}
```

### GET /models
Lists available models with latency/accuracy tradeoffs.

## Model Selection

| Model     | Size   | Speed   | Notes                                  |
|-----------|--------|---------|----------------------------------------|
| tiny.en   | 75MB   | fastest | Adequate for clean commands            |
| base.en   | 145MB  | fast    | **Recommended default**                |
| small.en  | 466MB  | medium  | Better with accents                    |
| medium    | 1.5GB  | slow    | Handles Maltese/Italian accents        |
| large-v3  | 3.1GB  | slowest | Best quality, shared with Kenneth      |

## Notes

- **Stub mode:** If faster-whisper isn't installed, the API starts in stub mode and returns mock transcripts. Useful for pipeline testing.
- **Port 8765** — chosen to avoid conflict with Mission Control (3002), Betabase (3003).
- **VAD filter enabled** — strips silence from audio automatically, speeds up transcription.
- **Auth:** Optional. Set `STT_API_KEY` in `/etc/environment` to require Bearer token.
- **Upload guardrails:** rejects unsupported media types with HTTP 415 and oversized uploads with HTTP 413. Configure the max payload via `STT_MAX_AUDIO_BYTES` (default 5 MiB).
- **Corrupted-audio handling:** decoder/transcription failures now return HTTP 400 instead of bubbling up as a server crash, so bad panel uploads don't poison the service.
- **16kHz mono WAV** is the ideal format from the Android panel. Other accepted formats: WebM, OGG, MP4/M4A, and MP3.
