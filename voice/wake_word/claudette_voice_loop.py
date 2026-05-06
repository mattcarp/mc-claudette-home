#!/usr/bin/env python3
"""
Claudette Voice Loop — Complete voice interaction pipeline.

This script combines:
1. Wake word detection (Porcupine)
2. Speech-to-text (Whisper on Workshop)
3. Intent processing (Claudette/OpenClaw)
4. Text-to-speech response (Gemini Kore)

Usage:
    python3 claudette_voice_loop.py --mode claudette  # Full pipeline
    python3 claudette_voice_loop.py --mode test       # Test with built-in keywords

Environment:
    PICOVOICE_ACCESS_KEY - Picovoice access key (get from console.picovoice.ai)
    CLAUDETTE_GATEWAY_URL - OpenClaw gateway URL (default: http://localhost:8080)
    STT_SERVICE_URL - STT service URL (default: http://localhost:8765)
"""

import argparse
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np
import requests

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from wake_word_detector import (
    WakeWordDetector,
    create_builtin_config,
    create_claudette_config,
    get_builtin_keywords,
)


class STTClient:
    """Client for the Whisper STT service."""

    def __init__(self, base_url: str = "http://localhost:8765", api_key: str | None = None):
        self.base_url = base_url
        self.api_key = api_key if api_key is not None else os.environ.get("STT_API_KEY", "")

    def health_check(self) -> bool:
        """Check if STT service is running."""
        try:
            response = requests.get(f"{self.base_url}/health", timeout=2)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def transcribe_audio(self, audio_data: bytes, content_type: str = "audio/wav") -> str:
        """
        Send audio to STT service and get transcription.

        Args:
            audio_data: Raw audio bytes
            content_type: MIME type of audio

        Returns:
            Transcribed text
        """
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = requests.post(
            f"{self.base_url}/transcribe",
            files={"audio": ("command.wav", audio_data, content_type)},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("text", "")


class VoiceLoop:
    """
    Complete voice interaction loop for Claudette Home.
    
    Flow:
        [Microphone] → Wake Word Detection → [Record Command] → 
        STT (Whisper) → Intent (Claudette) → TTS Response → [Speaker]
    """
    
    def __init__(
        self,
        wake_word_config,
        stt_url: str = "http://localhost:8765",
        gateway_url: str = "http://localhost:8080",
        record_seconds: float = 5.0,
    ):
        self.wake_detector = WakeWordDetector(wake_word_config)
        self.stt_client = STTClient(stt_url)
        self.gateway_url = gateway_url
        self.record_seconds = record_seconds
        self.is_running = False
        
    def initialize(self) -> bool:
        """Initialize all components."""
        print("Initializing Voice Loop...")
        
        # Initialize wake word detector
        if not self.wake_detector.initialize():
            print("ERROR: Failed to initialize wake word detector")
            return False
        print("✓ Wake word detector ready")
        
        # Check STT service
        if self.stt_client.health_check():
            print(f"✓ STT service at {self.stt_client.base_url}")
        else:
            print(f"⚠ STT service not available at {self.stt_client.base_url}")
            print("  (Will retry on first use)")
        
        return True
    
    def _record_after_wake(self) -> bytes:
        """
        Record audio after wake word detection.
        
        Returns:
            WAV file data as bytes
        """
        try:
            from pvrecorder import PvRecorder
            import io
            import wave
            
            # Calculate frames to record
            frame_length = self.wake_detector.porcupine.frame_length
            num_frames = int((self.record_seconds * 16000) / frame_length)
            
            recorder = PvRecorder(frame_length=frame_length)
            frames = []
            
            print(f"  [Recording for {self.record_seconds}s...]")
            recorder.start()
            
            for _ in range(num_frames):
                frame = recorder.read()
                frames.append(struct.pack('h' * len(frame), *frame))
                
            recorder.stop()
            recorder.delete()
            
            # Build WAV file in memory
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b''.join(frames))
            
            return wav_buffer.getvalue()
            
        except ImportError:
            print("ERROR: pvrecorder not installed")
            return b""
        except Exception as e:
            print(f"ERROR recording audio: {e}")
            return b""
    
    def _send_to_claudette(self, text: str) -> str:
        """
        Send transcribed text to Claudette and get response.
        
        Args:
            text: User's transcribed speech
            
        Returns:
            Claudette's response text
        """
        try:
            # TODO: Integrate with actual OpenClaw gateway
            # For now, return a placeholder response
            print(f"  [To Claudette]: {text}")
            
            # This would be a call to the OpenClaw gateway
            # response = requests.post(
            #     f"{self.gateway_url}/v1/chat",
            #     json={"message": text, "context": {"source": "voice"}},
            #     timeout=30,
            # )
            
            # Placeholder:
            return f"I heard you say: {text}. (Full integration pending)"
            
        except Exception as e:
            print(f"ERROR sending to Claudette: {e}")
            return "I'm having trouble connecting right now."
    
    def _play_response(self, text: str):
        """Play TTS response through speakers."""
        print(f"  [Claudette]: {text}")
        # TODO: Integrate with TTS service (Gemini Kore)
        # For now, just print the text
    
    def _on_wake_word(self, keyword_index: int):
        """Handle wake word detection."""
        print(f"\n{'='*50}")
        print("[WAKE WORD DETECTED] Starting voice interaction...")
        print(f"{'='*50}")
        
        # Record audio
        audio_data = self._record_after_wake()
        if not audio_data:
            print("ERROR: Failed to record audio")
            return
        
        # Transcribe
        try:
            if not self.stt_client.health_check():
                print("ERROR: STT service not available")
                return
                
            text = self.stt_client.transcribe_audio(audio_data)
            print(f"  [STT Result]: {text}")
            
        except Exception as e:
            print(f"ERROR during transcription: {e}")
            return
        
        # Send to Claudette
        response = self._send_to_claudette(text)
        
        # Play response
        self._play_response(response)
        
        print(f"{'='*50}\n")
    
    def run(self):
        """Start the voice loop."""
        self.is_running = True
        
        print("\n" + "="*50)
        print("Claudette Voice Loop Started")
        print("="*50)
        print("Say the wake word to begin interaction.")
        print("Press Ctrl+C to stop.\n")
        
        try:
            self.wake_detector.start_microphone_stream(
                callback=self._on_wake_word
            )
        except KeyboardInterrupt:
            print("\nStopping voice loop...")
        finally:
            self.is_running = False
    
    def release(self):
        """Release resources."""
        self.wake_detector.release()


def main():
    parser = argparse.ArgumentParser(description="Claudette Voice Loop")
    parser.add_argument("--mode", choices=["test", "claudette"], default="test",
                       help="Wake word mode")
    parser.add_argument("--model", type=str, help="Path to custom .ppn model")
    parser.add_argument("--stt-url", type=str, default="http://localhost:8765",
                       help="STT service URL")
    parser.add_argument("--gateway-url", type=str, default="http://localhost:8080",
                       help="OpenClaw gateway URL")
    parser.add_argument("--record-time", type=float, default=5.0,
                       help="Seconds to record after wake word")
    parser.add_argument("--list-keywords", action="store_true",
                       help="List available built-in keywords")
    
    args = parser.parse_args()
    
    if args.list_keywords:
        keywords = get_builtin_keywords()
        print("Available built-in keywords:")
        for kw in keywords:
            print(f"  - {kw}")
        return
    
    # Get access key
    access_key = os.environ.get("PICOVOICE_ACCESS_KEY")
    if not access_key:
        print("Error: PICOVOICE_ACCESS_KEY environment variable required")
        print("Get your free key at: https://console.picovoice.ai/")
        sys.exit(1)
    
    # Create wake word config
    if args.mode == "claudette":
        config = create_claudette_config(access_key, args.model)
    else:
        print("Test mode: Using 'porcupine' as wake word (say 'porcupine' to test)\n")
        config = create_builtin_config(["porcupine"], access_key=access_key)
    
    # Create and run voice loop
    loop = VoiceLoop(
        wake_word_config=config,
        stt_url=args.stt_url,
        gateway_url=args.gateway_url,
        record_seconds=args.record_time,
    )
    
    if not loop.initialize():
        sys.exit(1)
    
    try:
        loop.run()
    finally:
        loop.release()


if __name__ == "__main__":
    main()
