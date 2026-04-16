# Claudette Wake Word Integration

This directory is now **product-specific glue** for Claudette Home.

Reusable wake-word prototype code, training scripts, listeners, and model experiments were moved to:

`/home/sysop/projects/mc-audio-framework/prototypes/wakeword-python/`

## What stays here
- `claudette_voice_loop.py` — Claudette-specific integration flow
- `wake_word_bridge.py` — product glue / bridging
- `claudette-wake-word.service` — service/unit wiring for Claudette Home
- `models/` — product-local models if we later decide product packaging needs them

## Rule
If it could plausibly power another product besides Claudette, it belongs in `mc-audio-framework`.
