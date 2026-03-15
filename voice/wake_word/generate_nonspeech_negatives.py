#!/usr/bin/env python3
"""
Claudette Home — Non-Speech Negative Sample Generator

The root cause of the 'always-fires' model collapse:
  All current negative samples are gTTS speech clips.
  In the openWakeWord embedding space, all short speech clips cluster
  together (cosine similarity > 0.99), so any DNN on top collapses.

This script generates 600 diverse NON-SPEECH negative samples using
scipy/numpy: pink noise, white noise, sine tones, music-like chirps,
silence, clicks, and band-limited noise. These span the embedding space
in very different directions from speech, giving the DNN a fighting chance.

After running this, retrain with the improved train_claudette_v2.py.

Usage:
    python3 generate_nonspeech_negatives.py [--count 600] [--out-dir training_data/negative]
    python3 generate_nonspeech_negatives.py --test   # generate 20 samples only

Output:
    training_data/negative/ns_*.wav — non-speech negatives (16kHz mono)
"""

import argparse
import os
import random
import sys
import wave
from pathlib import Path

import numpy as np
import scipy.signal


SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUT_DIR = SCRIPT_DIR / "training_data" / "negative"
SR = 16000
CLIP_DURATION_S = 1.5  # seconds — similar to positive samples
CLIP_SAMPLES = int(SR * CLIP_DURATION_S)


# ---------------------------------------------------------------------------
# Noise generators
# ---------------------------------------------------------------------------

def white_noise(n_samples: int, amplitude: float = 0.3) -> np.ndarray:
    """Flat-spectrum white noise."""
    return (np.random.randn(n_samples) * amplitude).astype(np.float32)


def pink_noise(n_samples: int, amplitude: float = 0.3) -> np.ndarray:
    """
    Pink noise (1/f spectrum) — more natural-sounding than white.
    Uses the Voss-McCartney algorithm approximation.
    """
    # Approximate pink noise via IIR filter on white noise
    b = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786])
    a = np.array([1, -2.494956002, 2.017265875, -0.522189400])
    white = np.random.randn(n_samples + 200)
    pink = scipy.signal.lfilter(b, a, white)[200:]
    pink = pink / (np.max(np.abs(pink)) + 1e-8) * amplitude
    return pink.astype(np.float32)


def sine_tone(n_samples: int, freq_hz: float, amplitude: float = 0.3) -> np.ndarray:
    """Pure sine wave at given frequency."""
    t = np.linspace(0, n_samples / SR, n_samples, endpoint=False)
    return (np.sin(2 * np.pi * freq_hz * t) * amplitude).astype(np.float32)


def multitone(n_samples: int, amplitude: float = 0.2) -> np.ndarray:
    """Sum of random sine tones — approximates non-speech musical sound."""
    freqs = np.random.choice([100, 200, 300, 400, 500, 600, 800, 1000, 1200,
                               1500, 2000, 2500, 3000, 4000], size=random.randint(2, 6))
    result = np.zeros(n_samples, dtype=np.float32)
    for f in freqs:
        result += sine_tone(n_samples, f, amplitude / len(freqs))
    return result


def chirp(n_samples: int, f0_hz: float = 200, f1_hz: float = 3000,
          amplitude: float = 0.3) -> np.ndarray:
    """Linear frequency sweep (like a bird chirp or FM signal)."""
    t = np.linspace(0, n_samples / SR, n_samples, endpoint=False)
    return (scipy.signal.chirp(t, f0=f0_hz, f1=f1_hz, t1=CLIP_DURATION_S,
                                method='linear') * amplitude).astype(np.float32)


def band_noise(n_samples: int, low_hz: float, high_hz: float,
               amplitude: float = 0.3) -> np.ndarray:
    """Band-limited noise (bandpass-filtered white noise)."""
    white = np.random.randn(n_samples)
    nyq = SR / 2
    low = max(low_hz / nyq, 0.001)
    high = min(high_hz / nyq, 0.999)
    if low >= high:
        high = min(low + 0.1, 0.999)
    b, a = scipy.signal.butter(4, [low, high], btype='bandpass')
    filtered = scipy.signal.lfilter(b, a, white)
    filtered = filtered / (np.max(np.abs(filtered)) + 1e-8) * amplitude
    return filtered.astype(np.float32)


def clicks_and_pops(n_samples: int, amplitude: float = 0.3) -> np.ndarray:
    """Random impulses and clicks."""
    signal = np.zeros(n_samples, dtype=np.float32)
    n_clicks = random.randint(2, 15)
    positions = np.random.randint(0, n_samples, n_clicks)
    widths = np.random.randint(1, 50, n_clicks)
    for pos, w in zip(positions, widths):
        end = min(pos + w, n_samples)
        signal[pos:end] = np.random.uniform(-1, 1) * amplitude
    return signal


def silence_with_hum(n_samples: int, amplitude: float = 0.05) -> np.ndarray:
    """Near-silence with very low-level hum (50Hz power line)."""
    hum = sine_tone(n_samples, 50.0, amplitude * 0.5)
    noise = white_noise(n_samples, amplitude * 0.1)
    return (hum + noise).astype(np.float32)


def rain_noise(n_samples: int, amplitude: float = 0.15) -> np.ndarray:
    """Simulated rain: filtered noise + occasional drops."""
    # Rain = band-limited noise (1kHz–8kHz) + random spikes
    rain = band_noise(n_samples, 1000, min(7900, SR / 2 - 100), amplitude * 0.7)
    # Add some drop spikes
    n_drops = random.randint(5, 30)
    positions = np.random.randint(0, n_samples, n_drops)
    for pos in positions:
        end = min(pos + random.randint(1, 8), n_samples)
        rain[pos:end] += np.random.uniform(0, amplitude * 0.5)
    return rain.clip(-1, 1).astype(np.float32)


def keyboard_typing(n_samples: int, amplitude: float = 0.2) -> np.ndarray:
    """Simulated keyboard typing: short bursts of broadband noise."""
    signal = np.zeros(n_samples, dtype=np.float32)
    n_keystrokes = random.randint(3, 20)
    positions = sorted(np.random.randint(0, n_samples, n_keystrokes))
    for pos in positions:
        burst_len = random.randint(100, 500)
        end = min(pos + burst_len, n_samples)
        burst = white_noise(end - pos, amplitude * random.uniform(0.3, 1.0))
        # Apply quick attack-decay envelope
        env = np.exp(-np.linspace(0, 5, end - pos))
        signal[pos:end] += burst * env
    return signal.clip(-1, 1).astype(np.float32)


def frequency_warble(n_samples: int, amplitude: float = 0.25) -> np.ndarray:
    """Warbling tone — AM or FM modulated, sounds machine-like."""
    carrier_freq = random.uniform(200, 2000)
    mod_freq = random.uniform(2, 20)
    t = np.linspace(0, n_samples / SR, n_samples, endpoint=False)
    mod = np.sin(2 * np.pi * mod_freq * t)
    carrier = np.sin(2 * np.pi * carrier_freq * t + mod * random.uniform(0.5, 5.0))
    return (carrier * amplitude).astype(np.float32)


# ---------------------------------------------------------------------------
# Sample factory
# ---------------------------------------------------------------------------

GENERATORS = [
    ("white_noise", lambda: white_noise(CLIP_SAMPLES, random.uniform(0.1, 0.4))),
    ("pink_noise", lambda: pink_noise(CLIP_SAMPLES, random.uniform(0.1, 0.4))),
    ("sine_tone_low", lambda: sine_tone(CLIP_SAMPLES, random.uniform(80, 300), random.uniform(0.1, 0.35))),
    ("sine_tone_mid", lambda: sine_tone(CLIP_SAMPLES, random.uniform(300, 1500), random.uniform(0.1, 0.35))),
    ("sine_tone_high", lambda: sine_tone(CLIP_SAMPLES, random.uniform(1500, 6000), random.uniform(0.1, 0.35))),
    ("multitone", lambda: multitone(CLIP_SAMPLES, random.uniform(0.1, 0.3))),
    ("chirp_up", lambda: chirp(CLIP_SAMPLES, random.uniform(100, 500), random.uniform(2000, 6000))),
    ("chirp_down", lambda: chirp(CLIP_SAMPLES, random.uniform(2000, 6000), random.uniform(100, 500))),
    ("band_low", lambda: band_noise(CLIP_SAMPLES, 80, 500, random.uniform(0.1, 0.3))),
    ("band_mid", lambda: band_noise(CLIP_SAMPLES, 500, 2000, random.uniform(0.1, 0.3))),
    ("band_high", lambda: band_noise(CLIP_SAMPLES, 2000, 7500, random.uniform(0.1, 0.3))),
    ("clicks", lambda: clicks_and_pops(CLIP_SAMPLES, random.uniform(0.1, 0.4))),
    ("hum", lambda: silence_with_hum(CLIP_SAMPLES, random.uniform(0.02, 0.1))),
    ("rain", lambda: rain_noise(CLIP_SAMPLES, random.uniform(0.1, 0.3))),
    ("keyboard", lambda: keyboard_typing(CLIP_SAMPLES, random.uniform(0.1, 0.3))),
    ("warble", lambda: frequency_warble(CLIP_SAMPLES, random.uniform(0.1, 0.3))),
    # Combinations
    ("white_plus_tone", lambda: white_noise(CLIP_SAMPLES, 0.1) + sine_tone(CLIP_SAMPLES, random.uniform(200, 3000), 0.2)),
    ("pink_plus_chirp", lambda: pink_noise(CLIP_SAMPLES, 0.1) + chirp(CLIP_SAMPLES, 200, 4000, 0.2)),
    ("silence", lambda: np.zeros(CLIP_SAMPLES, dtype=np.float32) + white_noise(CLIP_SAMPLES, 0.005)),
]


def float32_to_int16(audio: np.ndarray) -> np.ndarray:
    """Convert float32 [-1, 1] to int16."""
    audio = np.clip(audio, -1.0, 1.0)
    return (audio * 32767).astype(np.int16)


def write_wav(path: Path, audio: np.ndarray, sr: int = SR):
    """Write mono int16 WAV."""
    pcm = float32_to_int16(audio)
    with wave.open(str(path), 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def generate_nonspeech_negatives(
    out_dir: Path,
    count: int = 600,
    verbose: bool = True,
) -> int:
    """Generate `count` non-speech negative WAV files into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Count existing ns_ files
    existing = list(out_dir.glob("ns_*.wav"))
    start_idx = len(existing)
    if start_idx > 0 and verbose:
        print(f"   Found {start_idx} existing non-speech negatives, generating more...")

    generated = 0
    idx = start_idx

    for i in range(count):
        name, gen_fn = random.choice(GENERATORS)
        try:
            audio = gen_fn()
            # Normalize to reasonable level
            peak = np.max(np.abs(audio))
            if peak > 0:
                audio = audio / peak * random.uniform(0.1, 0.9)

            filename = out_dir / f"ns_{idx:04d}_{name}.wav"
            write_wav(filename, audio)
            generated += 1
            idx += 1

            if verbose and (i + 1) % 100 == 0:
                print(f"   ✅ {i + 1}/{count} non-speech negatives generated")
        except Exception as e:
            if verbose:
                print(f"   ⚠️  Failed to generate {name}: {e}")

    return generated


def main():
    parser = argparse.ArgumentParser(
        description="Generate non-speech negative samples for wake word training"
    )
    parser.add_argument("--count", type=int, default=600,
                        help="Number of samples to generate (default: 600)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"Output directory (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--test", action="store_true",
                        help="Quick test: generate 20 samples only")
    args = parser.parse_args()

    count = 20 if args.test else args.count

    print("🦞 Claudette Home — Non-Speech Negative Generator")
    print(f"   Output: {args.out_dir}")
    print(f"   Target: {count} samples")
    print(f"   Types: {len(GENERATORS)} generator types")
    print(f"   Duration: {CLIP_DURATION_S}s each @ {SR}Hz mono")
    print()

    generated = generate_nonspeech_negatives(args.out_dir, count=count, verbose=True)

    print(f"\n✅ Done! Generated {generated} non-speech negative samples.")
    print(f"   Location: {args.out_dir}/ns_*.wav")
    print(f"\n📋 Next: retrain with train_claudette_v2.py")
    print(f"   python3 train_claudette_v2.py --steps 4000")


if __name__ == "__main__":
    main()
