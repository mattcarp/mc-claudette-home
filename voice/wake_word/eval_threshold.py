import sys, glob, wave, numpy as np
from openwakeword.model import Model

owm = Model(wakeword_models=["models/claudette.onnx"], inference_framework="onnx")

def get_max_score(filepath):
    try:
        with wave.open(filepath, 'rb') as f:
            audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        chunk_size = 1280
        max_score = 0.0
        for i in range(0, len(audio), chunk_size):
            chunk = audio[i:i+chunk_size]
            if len(chunk) < chunk_size: chunk = np.pad(chunk, (0, chunk_size-len(chunk)))
            score = owm.predict(chunk).get("claudette", 0.0)
            max_score = max(max_score, score)
        return max_score
    except Exception as e:
        return 0.0

pos_scores = [get_max_score(f) for f in glob.glob("training_data/positive/*.wav")]
neg_scores = [get_max_score(f) for f in glob.glob("training_data/negative/*.wav")]

print(f"Mean Pos Score: {np.mean(pos_scores):.4f} (min {np.min(pos_scores):.4f}, max {np.max(pos_scores):.4f})")
print(f"Mean Neg Score: {np.mean(neg_scores):.4f} (min {np.min(neg_scores):.4f}, max {np.max(neg_scores):.4f})")

thresholds = [0.1, 0.3, 0.5, 0.7, 0.9, 0.95, 0.99]
for t in thresholds:
    pos_acc = sum(s >= t for s in pos_scores) / len(pos_scores) * 100
    neg_acc = sum(s < t for s in neg_scores) / len(neg_scores) * 100
    print(f"Threshold {t:.2f} => Recall: {pos_acc:.1f}% | True Neg: {neg_acc:.1f}%")

