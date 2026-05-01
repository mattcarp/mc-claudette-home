# Claudette Wake Word Integration

This directory is now **product-specific glue** for Claudette Home.

Reusable wake-word prototype code, training scripts, listeners, and model experiments were moved to:

`/home/sysop/projects/mc-audio-framework/prototypes/wakeword-python/`

## What stays here
- `claudette_voice_loop.py` — Claudette-specific integration flow
- `wake_word_bridge.py` — product glue / bridging
- `claudette-wake-word.service` — service/unit wiring for Claudette Home
- `models/` — product-local models if we later decide product packaging needs them

## Stub backend for CI/dev
`wake_word_bridge.py` also supports a deterministic `stub` backend for no-hardware runs:

```bash
python3 voice/wake_word/wake_word_bridge.py --backend stub --max-events 3 --interval 0
```

It emits the normal listener lifecycle + `wake_word_detected` JSON events, so the rest of the Claudette Home voice pipeline can be exercised in tests without a microphone, Porcupine credentials, or an openWakeWord model.

## Rule
If it could plausibly power another product besides Claudette, it belongs in `mc-audio-framework`.
