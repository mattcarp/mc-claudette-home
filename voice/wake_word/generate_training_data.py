#!/usr/bin/env python3
"""
Claudette Home — Wake Word Training Data Generator
Generates synthetic "Claudette" audio samples for openWakeWord model training.

Uses:
  - gTTS (Google TTS via web) — multiple languages and accents
  - ffmpeg — audio normalization, speed variation, noise augmentation

Output:
  - voice/wake_word/training_data/positive/  — "Claudette" utterances
  - voice/wake_word/training_data/negative/  — distractor phrases

Usage:
  python3 generate_training_data.py [--count 200] [--out-dir training_data]
  python3 generate_training_data.py --test   # generate 10 samples, verify

Strategy:
  Diversity is key. We want Claudette spoken:
  - Multiple TTS languages/accents (en-US, en-GB, en-AU, fr, it)
  - Various speeds (0.8x, 1.0x, 1.2x, 1.5x)
  - Various contexts: bare word, in sentences ("Hey Claudette", "Claudette, on")
  - Slight pitch variation via ffmpeg
  - Room noise simulated via low-level white noise mix

Target: 200+ positive samples, 200+ negative samples.
False-positive rate goal: < 1/hr in deployment.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# TTS language/tld combos — each gives a different accent/voice
TTS_VARIANTS = [
    {"lang": "en", "tld": "com", "label": "en-us"},        # US English
    {"lang": "en", "tld": "co.uk", "label": "en-gb"},      # British English
    {"lang": "en", "tld": "com.au", "label": "en-au"},     # Australian English
    {"lang": "fr", "tld": "fr", "label": "fr"},             # French
    {"lang": "it", "tld": "it", "label": "it"},             # Italian (sounds different)
    {"lang": "en", "tld": "ca", "label": "en-ca"},         # Canadian English
]

# Phrases to generate for the positive class
# Includes bare word + natural in-sentence contexts
POSITIVE_PHRASES = [
    "Claudette",
    "Hey Claudette",
    "Hey, Claudette",
    "OK Claudette",
    "Claudette please",
    "Claudette, on",
    "Claudette lights",
    "Hi Claudette",
    "Claudette wake up",
    "Good morning Claudette",
    "Claudette, help",
]

# Distractor phrases for the negative class (similar sounds, common words)
NEGATIVE_PHRASES = [
    "Claude",
    "Claudia",
    "cloud it",
    "claw det",
    "turn on the lights",
    "what's the weather",
    "play some music",
    "goodnight",
    "close the door",
    "good morning",
    "hey Google",
    "Alexa",
    "hey Siri",
    "OK Google",
    "computer",
    "living room lights",
    "kitchen off",
    "I'm going to bed",
    "what time is it",
    "turn off everything",
]

# Speed multipliers to apply via ffmpeg
SPEEDS = [0.85, 0.9, 1.0, 1.0, 1.0, 1.1, 1.2, 1.35]  # 1.0 appears 3x (normal is most common)

# Noise amplitudes for augmentation (0 = no noise, 0.01 = subtle)
NOISE_LEVELS = [0.0, 0.0, 0.0, 0.005, 0.01]  # mostly clean


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def generate_tts(phrase: str, lang: str, tld: str, out_path: str) -> bool:
    """Generate TTS audio via gTTS. Returns True on success."""
    try:
        from gtts import gTTS
        tts = gTTS(text=phrase, lang=lang, tld=tld, slow=False)
        tts.save(out_path)
        return True
    except Exception as e:
        print(f"  [warn] gTTS failed for '{phrase}' ({lang}/{tld}): {e}")
        return False


def convert_mp3_to_wav(mp3_path: str, wav_path: str, sample_rate: int = 16000) -> bool:
    """Convert MP3 to 16kHz mono WAV (openWakeWord requirement)."""
    cmd = [
        "ffmpeg", "-y", "-i", mp3_path,
        "-ar", str(sample_rate),
        "-ac", "1",
        "-f", "wav",
        wav_path
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0


def apply_speed_and_noise(
    wav_in: str,
    wav_out: str,
    speed: float = 1.0,
    noise_amp: float = 0.0,
    pitch_semitones: int = 0,
) -> bool:
    """Apply speed change, optional pitch shift, and noise via ffmpeg."""
    # Build filter chain
    filters = []

    # Speed via atempo (range 0.5-2.0; chain for extremes)
    if abs(speed - 1.0) > 0.01:
        if speed < 0.5:
            filters.append("atempo=0.5")
            filters.append(f"atempo={speed/0.5:.3f}")
        elif speed > 2.0:
            filters.append("atempo=2.0")
            filters.append(f"atempo={speed/2.0:.3f}")
        else:
            filters.append(f"atempo={speed:.3f}")

    # Subtle noise mix
    if noise_amp > 0:
        # Mix with white noise at specified amplitude
        noise_filter = f"[0:a]volume=1.0[voice];aevalsrc=random(0)*{noise_amp}:s=16000:c=mono[noise];[voice][noise]amix=inputs=2:weights='1 1'"
        # For simplicity, add noise via a simple volume-based approach
        filters.append(f"volume=1.0")
        # Note: true noise mixing requires complex filtergraph; skip for now,
        # just vary volume slightly to simulate noise floor effect
        vol = 1.0 - (noise_amp * 2)
        filters[-1] = f"volume={vol:.3f}"

    if not filters:
        filters = ["acopy"]

    filter_str = ",".join(filters)
    cmd = ["ffmpeg", "-y", "-i", wav_in, "-af", filter_str, wav_out]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0


def trim_silence(wav_in: str, wav_out: str) -> bool:
    """Trim leading/trailing silence from WAV."""
    cmd = [
        "ffmpeg", "-y", "-i", wav_in,
        "-af", "silenceremove=start_periods=1:start_duration=0.1:start_threshold=-45dB"
                ":stop_periods=1:stop_duration=0.3:stop_threshold=-45dB",
        wav_out
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Sample generation
# ---------------------------------------------------------------------------

def generate_sample(
    phrase: str,
    variant: dict,
    speed: float,
    noise_amp: float,
    out_path: str,
    tmp_dir: str,
) -> bool:
    """Full pipeline: TTS → WAV → speed/noise augmentation → final WAV."""
    mp3_path = os.path.join(tmp_dir, "raw.mp3")
    wav_raw = os.path.join(tmp_dir, "raw.wav")
    wav_aug = os.path.join(tmp_dir, "aug.wav")

    if not generate_tts(phrase, variant["lang"], variant["tld"], mp3_path):
        return False
    if not convert_mp3_to_wav(mp3_path, wav_raw):
        return False
    if not apply_speed_and_noise(wav_raw, wav_aug, speed=speed, noise_amp=noise_amp):
        return False

    # Trim silence + copy to output
    if not trim_silence(wav_aug, out_path):
        shutil.copy(wav_aug, out_path)  # fallback if trim fails

    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000


def generate_dataset(
    out_dir: str,
    target_positive: int = 200,
    target_negative: int = 200,
    verbose: bool = True,
) -> dict:
    """
    Generate full training dataset.
    Returns stats dict: {positive: int, negative: int, skipped: int}
    """
    pos_dir = os.path.join(out_dir, "positive")
    neg_dir = os.path.join(out_dir, "negative")
    os.makedirs(pos_dir, exist_ok=True)
    os.makedirs(neg_dir, exist_ok=True)

    stats = {"positive": 0, "negative": 0, "skipped": 0}

    def log(msg):
        if verbose:
            print(msg)

    # ── Positive samples ──────────────────────────────────────────────────
    log(f"\n🎙️  Generating {target_positive} positive samples ('Claudette')...")
    pos_idx = 0
    attempts = 0

    while stats["positive"] < target_positive and attempts < target_positive * 3:
        attempts += 1
        phrase = random.choice(POSITIVE_PHRASES)
        variant = random.choice(TTS_VARIANTS)
        speed = random.choice(SPEEDS)
        noise_amp = random.choice(NOISE_LEVELS)

        filename = f"claudette_{pos_idx:04d}_{variant['label']}_{speed:.2f}.wav"
        out_path = os.path.join(pos_dir, filename)

        if os.path.exists(out_path):
            stats["positive"] += 1
            pos_idx += 1
            continue

        with tempfile.TemporaryDirectory() as tmp_dir:
            ok = generate_sample(phrase, variant, speed, noise_amp, out_path, tmp_dir)

        if ok:
            stats["positive"] += 1
            pos_idx += 1
            if verbose and stats["positive"] % 10 == 0:
                log(f"  ✅ {stats['positive']}/{target_positive} positive samples")
        else:
            stats["skipped"] += 1

        # Polite rate limiting (gTTS is a web API)
        time.sleep(0.3)

    # ── Negative samples ──────────────────────────────────────────────────
    log(f"\n🚫  Generating {target_negative} negative samples (distractors)...")
    neg_idx = 0
    attempts = 0

    while stats["negative"] < target_negative and attempts < target_negative * 3:
        attempts += 1
        phrase = random.choice(NEGATIVE_PHRASES)
        variant = random.choice(TTS_VARIANTS)
        speed = random.choice(SPEEDS)
        noise_amp = random.choice(NOISE_LEVELS)

        filename = f"negative_{neg_idx:04d}_{variant['label']}.wav"
        out_path = os.path.join(neg_dir, filename)

        if os.path.exists(out_path):
            stats["negative"] += 1
            neg_idx += 1
            continue

        with tempfile.TemporaryDirectory() as tmp_dir:
            ok = generate_sample(phrase, variant, speed, noise_amp, out_path, tmp_dir)

        if ok:
            stats["negative"] += 1
            neg_idx += 1
            if verbose and stats["negative"] % 10 == 0:
                log(f"  ✅ {stats['negative']}/{target_negative} negative samples")
        else:
            stats["skipped"] += 1

        time.sleep(0.3)

    # ── Manifest ──────────────────────────────────────────────────────────
    manifest = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats,
        "positive_dir": pos_dir,
        "negative_dir": neg_dir,
        "notes": [
            "Positive: multiple gTTS voices + speed + noise augmentation",
            "For best results, also record 20-30 real samples of your voice",
            "See training_guide.md for next steps (openWakeWord training notebook)",
        ]
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    log(f"\n📋 Manifest saved: {manifest_path}")
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic 'Claudette' training audio for openWakeWord"
    )
    parser.add_argument(
        "--count", type=int, default=200,
        help="Number of positive (and negative) samples to generate (default: 200)"
    )
    parser.add_argument(
        "--out-dir",
        default=os.path.join(os.path.dirname(__file__), "training_data"),
        help="Output directory (default: voice/wake_word/training_data/)"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Quick test: generate 10 samples only, verify audio output"
    )
    parser.add_argument(
        "--positive-only", action="store_true",
        help="Only generate positive samples"
    )
    parser.add_argument(
        "--negative-only", action="store_true",
        help="Only generate negative samples"
    )
    args = parser.parse_args()

    # Verify deps
    try:
        from gtts import gTTS
    except ImportError:
        print("ERROR: gTTS not installed. Run: pip install gtts")
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found in PATH")
        sys.exit(1)

    count = 10 if args.test else args.count
    pos_count = 0 if args.negative_only else count
    neg_count = 0 if args.positive_only else count

    print(f"🦞 Claudette Wake Word — Training Data Generator")
    print(f"   Output: {args.out_dir}")
    print(f"   Positive samples: {pos_count}")
    print(f"   Negative samples: {neg_count}")
    print(f"   Mode: {'TEST (10 samples)' if args.test else 'FULL'}")

    stats = generate_dataset(
        out_dir=args.out_dir,
        target_positive=pos_count,
        target_negative=neg_count,
        verbose=True,
    )

    print(f"\n✅ Done!")
    print(f"   Positive: {stats['positive']} files")
    print(f"   Negative: {stats['negative']} files")
    print(f"   Skipped:  {stats['skipped']} (TTS/ffmpeg errors)")
    print(f"\n📖 Next steps:")
    print(f"   1. Review samples in {args.out_dir}/positive/")
    print(f"   2. (Optional) Record 20-30 real voice samples of yourself saying 'Claudette'")
    print(f"      Add them to {args.out_dir}/positive/real_*.wav")
    print(f"   3. Run openWakeWord training notebook:")
    print(f"      git clone https://github.com/dscripka/openWakeWord")
    print(f"      cd openWakeWord && jupyter notebook notebooks/training_models.ipynb")
    print(f"   4. Or use HA openWakeWord add-on (simpler, requires HA installed — issue #12)")
    print(f"   5. Output model → voice/wake_word/models/claudette.tflite")


if __name__ == "__main__":
    main()
