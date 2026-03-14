import sys
import glob
from pathlib import Path
import numpy as np
from openwakeword.model import Model
import wave

def test_model():
    model_path = "models/claudette.onnx"
    print(f"Loading model {model_path}...")
    owm = Model(wakeword_models=[model_path], inference_framework="onnx")
    
    pos_files = glob.glob("training_data/positive/*.wav")
    neg_files = glob.glob("training_data/negative/*.wav")
    
    if not pos_files or not neg_files:
        print("Missing training data.")
        sys.exit(1)
        
    pos_file = pos_files[0]
    neg_file = neg_files[0]
    
    def test_file(filepath):
        print(f"\nTesting {filepath}:")
        with wave.open(filepath, 'rb') as f:
            frames = f.readframes(f.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16)
        
        chunk_size = 1280
        max_score = 0.0
        detected = False
        for i in range(0, len(audio), chunk_size):
            chunk = audio[i:i+chunk_size]
            if len(chunk) < chunk_size:
                chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
            prediction = owm.predict(chunk)
            score = prediction.get("claudette", 0.0)
            if score > max_score:
                max_score = score
            if score > 0.5 and not detected:
                print(f"  🔥 WAKE WORD DETECTED! Score: {score:.4f}")
                detected = True
                
        if not detected:
            print(f"  ❌ No wake word detected. Max score: {max_score:.4f}")
        return detected, max_score

    print("--- Positive Sample ---")
    test_file(pos_file)
    print("--- Negative Sample ---")
    test_file(neg_file)

if __name__ == "__main__":
    test_model()
