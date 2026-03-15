#!/usr/bin/env python3
"""
Claudette Home — Wake Word Threshold Evaluator v2

Evaluates the model against HELD-OUT samples (not the training data).
Uses a random 20% of samples as the test set (seeded for reproducibility).

Shows per-threshold recall/FP-rate and recommends the best threshold.

Usage:
    python3 eval_threshold_v2.py [--threshold 0.5] [--model models/claudette.onnx]
"""

import argparse
import glob
import random
import sys
import wave
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent.resolve()


def load_holdout(data_dir: Path, holdout_frac: float = 0.2, seed: int = 42):
    """Return held-out positive and negative file paths."""
    rng = random.Random(seed)

    pos_files = sorted(glob.glob(str(data_dir / "positive" / "*.wav")))
    neg_files = sorted(glob.glob(str(data_dir / "negative" / "*.wav")))

    def holdout(files):
        files = list(files)
        rng.shuffle(files)
        n = max(1, int(len(files) * holdout_frac))
        return files[-n:]  # last 20% = held-out

    return holdout(pos_files), holdout(neg_files)


def score_file(filepath: str, owm, model_name: str = "claudette", chunk_size: int = 1280) -> float:
    """Return the maximum score across all chunks of a WAV file."""
    try:
        with wave.open(filepath, 'rb') as f:
            audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        max_score = 0.0
        for i in range(0, len(audio), chunk_size):
            chunk = audio[i:i + chunk_size]
            if len(chunk) < chunk_size:
                chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
            score = owm.predict(chunk).get(model_name, 0.0)
            max_score = max(max_score, score)
        return max_score
    except Exception:
        return 0.0


def main():
    parser = argparse.ArgumentParser(description="Evaluate wake word model on held-out test set")
    parser.add_argument("--model", default="models/claudette.onnx")
    parser.add_argument("--data-dir", type=Path, default=SCRIPT_DIR / "training_data")
    parser.add_argument("--holdout-frac", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print("🏠 Claudette Home — Wake Word Evaluator v2")
    print(f"   Model: {args.model}")
    print(f"   Held-out fraction: {args.holdout_frac:.0%}")
    print()

    # Load model
    try:
        from openwakeword.model import Model
        owm = Model(wakeword_models=[args.model], inference_framework="onnx")
        print(f"✅ Model loaded")
    except Exception as e:
        print(f"❌ Failed to load model: {e}")
        sys.exit(1)

    # Load held-out files
    pos_files, neg_files = load_holdout(args.data_dir, args.holdout_frac, args.seed)
    print(f"📋 Test set: {len(pos_files)} positive, {len(neg_files)} negative (held-out {args.holdout_frac:.0%})")
    print()

    # Score all files
    print("Scoring positive files...")
    pos_scores = []
    for i, f in enumerate(pos_files):
        score = score_file(f, owm)
        pos_scores.append(score)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(pos_files)}")

    print("Scoring negative files...")
    neg_scores = []
    for i, f in enumerate(neg_files):
        score = score_file(f, owm)
        neg_scores.append(score)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(neg_files)}")

    pos_scores = np.array(pos_scores)
    neg_scores = np.array(neg_scores)

    # Speech-only negatives
    neg_files_ns = [f for f in neg_files if Path(f).name.startswith("ns_")]
    neg_files_speech = [f for f in neg_files if not Path(f).name.startswith("ns_")]
    neg_scores_ns = np.array([neg_scores[neg_files.index(f)] for f in neg_files_ns]) if neg_files_ns else np.array([])
    neg_scores_speech = np.array([neg_scores[neg_files.index(f)] for f in neg_files_speech]) if neg_files_speech else np.array([])

    print()
    print("📊 Score Distribution (HELD-OUT TEST SET)")
    print(f"   Positive — mean={pos_scores.mean():.4f} | min={pos_scores.min():.4f} | max={pos_scores.max():.4f}")
    print(f"   All neg  — mean={neg_scores.mean():.4f} | min={neg_scores.min():.4f} | max={neg_scores.max():.4f}")
    if len(neg_scores_speech) > 0:
        print(f"   Speech neg  — mean={neg_scores_speech.mean():.4f} (n={len(neg_scores_speech)})")
    if len(neg_scores_ns) > 0:
        print(f"   Non-speech neg — mean={neg_scores_ns.mean():.4f} (n={len(neg_scores_ns)})")
    print()

    print(f"{'Threshold':>10} | {'Recall':>8} | {'FP Rate':>8} | {'True Neg':>8} | {'Score':>8}")
    print("-" * 55)

    best_threshold = 0.5
    best_score = -999
    thresholds = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99]

    for t in thresholds:
        recall = float((pos_scores >= t).mean())
        fp_rate = float((neg_scores >= t).mean())
        true_neg = 1.0 - fp_rate
        score = recall - 2 * fp_rate  # penalize FP heavily

        marker = ""
        if score > best_score and recall > 0.5 and fp_rate < 0.3:
            best_score = score
            best_threshold = t
            marker = " ← best"

        print(f"   {t:>7.2f}  | {recall:>7.1%}  | {fp_rate:>7.1%}  | {true_neg:>7.1%}  | {score:>7.3f}{marker}")

    print()
    print(f"🎯 Recommended threshold: {best_threshold}")
    print(f"   At threshold {best_threshold}:")
    rec = float((pos_scores >= best_threshold).mean())
    fp = float((neg_scores >= best_threshold).mean())
    print(f"   → Recall (true wake words detected): {rec:.1%}")
    print(f"   → False positive rate: {fp:.1%}")

    if rec < 0.5:
        print("\n⚠️  WARNING: Low recall. Model may not be firing on real wake words.")
        print("   → Retrain with more positive samples or lower dropout")
    if fp > 0.1:
        print("\n⚠️  WARNING: High false positive rate (>10%). In-home use will be annoying.")
        print("   → Add more non-speech negatives and retrain")
    if abs(pos_scores.mean() - neg_scores.mean()) < 0.05:
        print("\n❌ COLLAPSE DETECTED: Positive and negative scores are nearly identical.")
        print("   → Model has not learned. Likely need more diverse non-speech negatives.")
        print("   → Run: python3 generate_nonspeech_negatives.py --count 1000")

    if rec >= 0.7 and fp < 0.05:
        print("\n✅ Model looks good! Update oww_listener.py threshold to:", best_threshold)


if __name__ == "__main__":
    main()
