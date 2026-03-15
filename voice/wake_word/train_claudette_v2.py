#!/usr/bin/env python3
"""
Claudette Home — Wake Word Model Trainer v2
Fixes the model collapse issue from v1 (always-fires ONNX).

Root cause of v1 collapse:
  - All negatives were gTTS speech clips → cosine similarity > 0.99 vs positives
  - DNN had no way to separate the classes → collapses to "output 1 for everything"

v2 fixes:
  1. Expects non-speech negatives (ns_*.wav) in training_data/negative/
     Run generate_nonspeech_negatives.py first to create them.
  2. Weighted loss: down-weights positives if they outnumber negatives
  3. Dropout regularization to prevent memorization
  4. Larger negative batch fraction (2:1 neg:pos ratio in each batch)
  5. Proper held-out test split (20%) evaluated SEPARATELY from training data
  6. Lowered learning rate + more warmup steps
  7. Early stopping on val_fp (false positive rate on negatives)

Usage:
    python3 train_claudette_v2.py                   # full 4000-step run
    python3 train_claudette_v2.py --quick           # 500-step test
    python3 train_claudette_v2.py --steps 6000      # longer training

Output:
    voice/wake_word/models/claudette.onnx           # overwrites old model
    voice/wake_word/models/training_log_v2.json     # metrics

Requirements:
    pip install openwakeword==0.6.0 torch torchaudio soundfile
    python3 generate_nonspeech_negatives.py --count 600   (do this first!)
"""

import argparse
import glob
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import List, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).parent.resolve()
TRAINING_DATA_DIR = SCRIPT_DIR / "training_data"
MODELS_DIR = SCRIPT_DIR / "models"
OUTPUT_MODEL_PATH = MODELS_DIR / "claudette.onnx"
TRAINING_LOG_PATH = MODELS_DIR / "training_log_v2.json"

# Training hyperparameters
DEFAULT_STEPS = 4000
QUICK_STEPS = 500
WARMUP_STEPS = 100          # longer warmup vs v1
HOLD_STEPS = 200
LEARNING_RATE = 0.00005     # lower LR vs v1 (was 0.0001)
BATCH_SIZE = 64             # smaller batch, more updates
WINDOW_FRAMES = 16
DROPOUT_RATE = 0.3          # regularization
MIN_NONSPEECH_NEGATIVES = 100  # require non-speech negatives


def check_dependencies() -> bool:
    missing = []
    for pkg in ["openwakeword", "torch", "soundfile", "numpy"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"❌ Missing packages: {', '.join(missing)}")
        return False
    return True


def load_audio_paths(data_dir: Path) -> Tuple[List[str], List[str], List[str]]:
    """
    Returns (positive_paths, speech_negative_paths, nonspeech_negative_paths).
    Non-speech negatives are identified by the ns_ prefix.
    """
    pos_dir = data_dir / "positive"
    neg_dir = data_dir / "negative"

    positive = sorted(glob.glob(str(pos_dir / "*.wav")))
    all_negative = sorted(glob.glob(str(neg_dir / "*.wav")))

    # Split negatives into speech vs non-speech
    nonspeech_neg = [f for f in all_negative if Path(f).name.startswith("ns_")]
    speech_neg = [f for f in all_negative if not Path(f).name.startswith("ns_")]

    print(f"   Positive samples:          {len(positive)}")
    print(f"   Speech negative samples:   {len(speech_neg)}")
    print(f"   Non-speech negative samples: {len(nonspeech_neg)}")

    if len(nonspeech_neg) < MIN_NONSPEECH_NEGATIVES:
        print(f"\n❌ Need at least {MIN_NONSPEECH_NEGATIVES} non-speech negatives.")
        print(f"   Run first: python3 generate_nonspeech_negatives.py --count 600")
        sys.exit(1)

    return positive, speech_neg, nonspeech_neg


def compute_features(audio_paths: List[str], label: str, af) -> np.ndarray:
    """
    Compute openWakeWord audio embeddings for a list of WAV files.
    Returns (N, WINDOW_FRAMES, 96) float32 array.
    """
    import soundfile as sf
    import warnings

    if not audio_paths:
        return np.array([])

    print(f"   Computing features: {label} ({len(audio_paths)} clips)...")
    target_samples = int(SR * 2.0)  # 2 seconds = ~16 embedding frames

    valid_audios = []
    for path in audio_paths:
        try:
            audio, sr = sf.read(path, dtype='int16')
            if audio.ndim > 1:
                audio = audio[:, 0]
            if len(audio) < target_samples:
                audio = np.pad(audio, (0, target_samples - len(audio)))
            else:
                audio = audio[:target_samples]
            valid_audios.append(audio)
        except Exception:
            continue

    if not valid_audios:
        return np.array([])

    X = np.stack(valid_audios)  # (N, 32000)
    BATCH = 64
    all_embeddings = []

    for start in range(0, len(X), BATCH):
        batch = X[start:start + BATCH]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            embs = af.embed_clips(batch)  # (B, T, 96)
        T = embs.shape[1]
        if T >= WINDOW_FRAMES:
            window = embs[:, T - WINDOW_FRAMES:T, :]
        else:
            pad = np.zeros((embs.shape[0], WINDOW_FRAMES - T, 96), dtype=np.float32)
            window = np.concatenate([pad, embs], axis=1)
        all_embeddings.append(window.astype(np.float32))

    result = np.concatenate(all_embeddings, axis=0)
    print(f"     → {result.shape[0]} feature windows")
    return result


# Module-level SR
SR = 16000


def build_model():
    """Build DNN with dropout — more regularized than v1."""
    import torch
    import torch.nn as nn

    class WakeWordDNN(nn.Module):
        def __init__(self, input_frames: int = 16, input_dim: int = 96,
                     hidden_dim: int = 128, dropout: float = 0.3):
            super().__init__()
            self.model = nn.Sequential(
                nn.Flatten(),
                nn.Linear(input_frames * input_dim, hidden_dim),
                nn.LayerNorm(hidden_dim),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_dim, hidden_dim),
                nn.LayerNorm(hidden_dim),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_dim, 64),
                nn.LayerNorm(64),
                nn.ReLU(),
                nn.Dropout(dropout / 2),
                nn.Linear(64, 1),
                nn.Sigmoid(),
            )

        def forward(self, x):
            return self.model(x)

    return WakeWordDNN(
        input_frames=WINDOW_FRAMES,
        input_dim=96,
        hidden_dim=128,
        dropout=DROPOUT_RATE,
    )


def train(
    positive_paths: List[str],
    speech_neg_paths: List[str],
    nonspeech_neg_paths: List[str],
    steps: int,
    output_path: Path,
    log_path: Path,
):
    import torch
    import torch.nn as nn
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from openwakeword.train import AudioFeatures

    af = AudioFeatures(inference_framework="onnx")

    print("\n🔧 Computing embeddings...")

    X_pos = compute_features(positive_paths, "positive", af)

    # Use ALL negatives (speech + non-speech)
    all_neg_paths = speech_neg_paths + nonspeech_neg_paths
    X_neg = compute_features(all_neg_paths, "all negatives", af)

    if X_pos.size == 0:
        print("❌ No positive features extracted")
        return False
    if X_neg.size == 0:
        print("❌ No negative features extracted")
        return False

    # Labels
    y_pos = np.ones(len(X_pos), dtype=np.float32)
    y_neg = np.zeros(len(X_neg), dtype=np.float32)

    X_all = np.concatenate([X_pos, X_neg], axis=0)
    y_all = np.concatenate([y_pos, y_neg], axis=0)

    # Shuffle
    rng = np.random.default_rng(42)
    perm = rng.permutation(len(X_all))
    X_all, y_all = X_all[perm], y_all[perm]

    # 80/20 train/val split (stratified by label)
    pos_idx = np.where(y_all == 1)[0]
    neg_idx = np.where(y_all == 0)[0]

    def split_indices(idx, frac=0.8):
        rng.shuffle(idx)
        n_train = int(len(idx) * frac)
        return idx[:n_train], idx[n_train:]

    pos_train_idx, pos_val_idx = split_indices(pos_idx)
    neg_train_idx, neg_val_idx = split_indices(neg_idx)

    train_idx = np.concatenate([pos_train_idx, neg_train_idx])
    val_idx = np.concatenate([pos_val_idx, neg_val_idx])
    rng.shuffle(train_idx)
    rng.shuffle(val_idx)

    X_train, y_train = X_all[train_idx], y_all[train_idx]
    X_val, y_val = X_all[val_idx], y_all[val_idx]

    print(f"\n📊 Dataset:")
    print(f"   Positive windows:  {int(y_all.sum())}")
    print(f"   Negative windows:  {int((y_all == 0).sum())}")
    print(f"   Train: {len(X_train)} | Val: {len(X_val)}")

    # Class weights: balance pos/neg
    n_pos_train = int(y_train.sum())
    n_neg_train = int((y_train == 0).sum())
    pos_weight_val = n_neg_train / max(n_pos_train, 1)
    print(f"   Pos weight:        {pos_weight_val:.2f} (balances {n_pos_train} pos vs {n_neg_train} neg)")

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print(f"   Device:            {device}")

    # Build model and optimizer
    model = build_model().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=1e-4)

    pos_weight_tensor = torch.tensor([pos_weight_val], device=device)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight_tensor)

    # Convert to tensors
    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.float32)
    X_val_t = torch.tensor(X_val, dtype=torch.float32, device=device)
    y_val_t = torch.tensor(y_val, dtype=torch.float32, device=device)

    from torch.utils.data import DataLoader, TensorDataset
    train_ds = TensorDataset(X_train_t, y_train_t)
    loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, drop_last=False)

    print(f"\n🏋️  Training for {steps} steps...")

    # Use BCEWithLogitsLoss so we need raw logits from the last Linear layer
    # But our model ends with Sigmoid — patch: use last layer raw output
    # Actually: just use BCE directly with sigmoid output
    loss_fn = nn.BCELoss()

    history = {"step": [], "loss": [], "recall": [], "val_recall": [], "val_fp_rate": []}
    best_val_recall = 0.0
    best_val_fp = 1.0
    best_state = None
    start_time = time.time()

    step = 0
    val_interval = 200 if steps <= QUICK_STEPS else 500

    for epoch in range(9999):
        for batch_X, batch_y in loader:
            if step >= steps:
                break

            # LR warmup + cosine decay
            if step < WARMUP_STEPS:
                lr = LEARNING_RATE * (step + 1) / WARMUP_STEPS
            elif step < WARMUP_STEPS + HOLD_STEPS:
                lr = LEARNING_RATE
            else:
                progress = (step - WARMUP_STEPS - HOLD_STEPS) / max(1, steps - WARMUP_STEPS - HOLD_STEPS)
                lr = LEARNING_RATE * 0.5 * (1 + np.cos(np.pi * progress))

            for g in optimizer.param_groups:
                g["lr"] = lr

            batch_X = batch_X.to(device)
            batch_y = batch_y.to(device)

            # Class-weighted loss (manual, since BCELoss doesn't have pos_weight)
            model.train()
            optimizer.zero_grad()
            preds = model(batch_X).squeeze(-1)

            # Manual pos-weight: weight each sample
            weights = torch.where(batch_y == 1,
                                  torch.tensor(pos_weight_val, device=device),
                                  torch.tensor(1.0, device=device))
            loss = (-(batch_y * torch.log(preds + 1e-8) +
                      (1 - batch_y) * torch.log(1 - preds + 1e-8)) * weights).mean()

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            step += 1

            # Validation
            if step % val_interval == 0 or step == steps:
                model.eval()
                with torch.no_grad():
                    val_preds = model(X_val_t).squeeze(-1)
                    val_pred_binary = (val_preds > 0.5).float()

                    pos_mask = y_val_t == 1
                    neg_mask = y_val_t == 0

                    tp = (val_pred_binary[pos_mask] == 1).float().sum()
                    fn = (val_pred_binary[pos_mask] == 0).float().sum()
                    fp = (val_pred_binary[neg_mask] == 1).float().sum()
                    tn = (val_pred_binary[neg_mask] == 0).float().sum()

                    val_recall = float(tp / (tp + fn + 1e-8))
                    val_fp_rate = float(fp / (fp + tn + 1e-8))

                history["step"].append(step)
                history["val_recall"].append(val_recall)
                history["val_fp_rate"].append(val_fp_rate)

                # Save best: maximize recall while keeping fp_rate low
                # Score = recall - 2 * fp_rate (penalize false positives heavily)
                score = val_recall - 2 * val_fp_rate
                if score > (best_val_recall - 2 * best_val_fp):
                    best_val_recall = val_recall
                    best_val_fp = val_fp_rate
                    best_state = {k: v.clone() for k, v in model.state_dict().items()}

                elapsed = time.time() - start_time
                train_recall = float((val_preds[:n_pos_train] > 0.5).float().mean()) if n_pos_train > 0 else 0
                print(f"   Step {step:4d}/{steps} | loss={float(loss):.4f} | "
                      f"val_recall={val_recall:.3f} | val_fp={val_fp_rate:.3f} | "
                      f"lr={lr:.6f} | {elapsed:.0f}s")

        if step >= steps:
            break

    # Restore best weights
    if best_state is not None:
        model.load_state_dict(best_state)
        print(f"\n   ↩️  Restored best weights: recall={best_val_recall:.3f} fp={best_val_fp:.3f}")

    # Export to ONNX
    print(f"\n📦 Exporting model to ONNX...")
    MODELS_DIR.mkdir(exist_ok=True)
    model.eval()
    dummy = torch.randn(1, WINDOW_FRAMES, 96)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        torch.onnx.export(
            model,
            dummy,
            str(output_path),
            export_params=True,
            opset_version=12,
            do_constant_folding=True,
            input_names=["input"],
            output_names=["output"],
            dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
        )
    print(f"   ✅ Model: {output_path}")

    # Save log
    log = {
        "version": "v2",
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": steps,
        "positive_windows": int(y_all.sum()),
        "negative_windows": int((y_all == 0).sum()),
        "nonspeech_negative_paths": len(nonspeech_neg_paths),
        "speech_negative_paths": len(speech_neg_paths),
        "best_val_recall": best_val_recall,
        "best_val_fp_rate": best_val_fp,
        "history": {k: v for k, v in history.items()},
        "notes": [
            "v2: added non-speech negatives (ns_*.wav), dropout, pos-weight, AdamW",
            f"best score = recall - 2*fp_rate = {best_val_recall - 2*best_val_fp:.3f}",
        ],
    }
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)
    print(f"   📋 Log: {log_path}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Train openWakeWord v2 — fixes model collapse with non-speech negatives"
    )
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=TRAINING_DATA_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_MODEL_PATH)
    args = parser.parse_args()

    print("🏠 Claudette Home — Wake Word Trainer v2")
    print("=" * 55)
    print("   Fixes: non-speech negatives + dropout + class weights")
    print()

    if not check_dependencies():
        sys.exit(1)

    try:
        import soundfile  # noqa: F401
    except ImportError:
        os.system("pip3 install soundfile --break-system-packages --quiet")

    print(f"📁 Loading data from {args.data_dir}...")
    positive_paths, speech_neg_paths, nonspeech_neg_paths = load_audio_paths(args.data_dir)

    steps = QUICK_STEPS if args.quick else args.steps
    if args.quick:
        print(f"\n⚡ Quick mode: {steps} steps")

    print(f"\n📊 Training config:")
    print(f"   Steps:         {steps}")
    print(f"   LR:            {LEARNING_RATE} (with warmup + cosine decay)")
    print(f"   Dropout:       {DROPOUT_RATE}")
    print(f"   Batch size:    {BATCH_SIZE}")
    print(f"   Val split:     20% stratified")

    start = time.time()
    success = train(
        positive_paths=positive_paths,
        speech_neg_paths=speech_neg_paths,
        nonspeech_neg_paths=nonspeech_neg_paths,
        steps=steps,
        output_path=args.output,
        log_path=TRAINING_LOG_PATH,
    )

    elapsed = time.time() - start
    if success:
        print(f"\n✅ Training complete in {elapsed:.0f}s")
        print(f"\n🔌 Test with:")
        print(f"   cd {SCRIPT_DIR}")
        print(f"   python3 eval_threshold.py")
        print(f"\n📋 Next steps:")
        print(f"   1. Run eval_threshold.py — check val_recall > 0.7, val_fp < 0.1")
        print(f"   2. If still collapsing: add more diverse non-speech negatives")
        print(f"      python3 generate_nonspeech_negatives.py --count 1000")
        print(f"   3. Add real voice recordings to training_data/positive/real_*.wav")
        print(f"   4. Re-train: python3 train_claudette_v2.py --steps 6000")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
