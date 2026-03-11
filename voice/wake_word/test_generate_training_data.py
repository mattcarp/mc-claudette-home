#!/usr/bin/env python3
"""
Tests for generate_training_data.py
Verifies audio generation pipeline: gTTS → ffmpeg → 16kHz mono WAV.
No hardware required. Hits gTTS web API (real calls, but tiny).
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

WAKE_DIR = os.path.dirname(__file__)
sys.path.insert(0, WAKE_DIR)

import generate_training_data as gtd


def wav_info(path: str) -> dict:
    """Parse WAV header to extract sample_rate, channels, bits."""
    with open(path, "rb") as f:
        header = f.read(44)
    if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return {}
    channels = struct.unpack_from("<H", header, 22)[0]
    sample_rate = struct.unpack_from("<I", header, 24)[0]
    bits = struct.unpack_from("<H", header, 34)[0]
    return {"channels": channels, "sample_rate": sample_rate, "bits": bits}


class TestConstants(unittest.TestCase):
    def test_positive_phrases_nonempty(self):
        self.assertGreater(len(gtd.POSITIVE_PHRASES), 5)

    def test_claudette_in_positive_phrases(self):
        self.assertIn("Claudette", gtd.POSITIVE_PHRASES)

    def test_negative_phrases_nonempty(self):
        self.assertGreater(len(gtd.NEGATIVE_PHRASES), 5)

    def test_tts_variants_nonempty(self):
        self.assertGreater(len(gtd.TTS_VARIANTS), 3)

    def test_each_variant_has_required_keys(self):
        for v in gtd.TTS_VARIANTS:
            self.assertIn("lang", v, f"Missing 'lang' in {v}")
            self.assertIn("tld", v, f"Missing 'tld' in {v}")
            self.assertIn("label", v, f"Missing 'label' in {v}")

    def test_speeds_are_reasonable(self):
        for s in gtd.SPEEDS:
            self.assertGreater(s, 0.5, f"Speed {s} too slow")
            self.assertLess(s, 2.0, f"Speed {s} too fast")


class TestTTSGeneration(unittest.TestCase):
    """Live gTTS calls — generates tiny audio files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gtts_us_english(self):
        out = os.path.join(self.tmp, "test_us.mp3")
        ok = gtd.generate_tts("Claudette", lang="en", tld="com", out_path=out)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 500, "MP3 too small — probably empty")

    def test_gtts_french(self):
        out = os.path.join(self.tmp, "test_fr.mp3")
        ok = gtd.generate_tts("Claudette", lang="fr", tld="fr", out_path=out)
        self.assertTrue(ok)
        self.assertGreater(os.path.getsize(out), 500)

    def test_gtts_british(self):
        out = os.path.join(self.tmp, "test_gb.mp3")
        ok = gtd.generate_tts("Hey Claudette", lang="en", tld="co.uk", out_path=out)
        self.assertTrue(ok)
        self.assertGreater(os.path.getsize(out), 500)


class TestFFmpegConversion(unittest.TestCase):
    """Tests MP3 → WAV conversion. Requires ffmpeg."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Generate one MP3 to use for conversion tests
        self.mp3_path = os.path.join(self.tmp, "source.mp3")
        ok = gtd.generate_tts("Claudette", lang="en", tld="com", out_path=self.mp3_path)
        if not ok:
            self.skipTest("gTTS unavailable — skipping FFmpeg tests")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mp3_to_wav_produces_file(self):
        wav = os.path.join(self.tmp, "out.wav")
        ok = gtd.convert_mp3_to_wav(self.mp3_path, wav)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(wav))
        self.assertGreater(os.path.getsize(wav), 100)

    def test_wav_is_16khz_mono(self):
        wav = os.path.join(self.tmp, "out.wav")
        gtd.convert_mp3_to_wav(self.mp3_path, wav)
        info = wav_info(wav)
        self.assertEqual(info.get("sample_rate"), 16000, "Sample rate should be 16000 Hz")
        self.assertEqual(info.get("channels"), 1, "Should be mono (1 channel)")

    def test_wav_header_valid(self):
        wav = os.path.join(self.tmp, "out.wav")
        gtd.convert_mp3_to_wav(self.mp3_path, wav)
        with open(wav, "rb") as f:
            header = f.read(12)
        self.assertEqual(header[:4], b"RIFF", "Not a RIFF file")
        self.assertEqual(header[8:12], b"WAVE", "Not a WAVE file")

    def test_speed_augmentation_slow(self):
        wav_in = os.path.join(self.tmp, "raw.wav")
        wav_out = os.path.join(self.tmp, "slow.wav")
        gtd.convert_mp3_to_wav(self.mp3_path, wav_in)
        ok = gtd.apply_speed_and_noise(wav_in, wav_out, speed=0.85)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(wav_out))
        # Slow = bigger file (more samples)
        self.assertGreater(os.path.getsize(wav_out), os.path.getsize(wav_in) * 0.8)

    def test_speed_augmentation_fast(self):
        wav_in = os.path.join(self.tmp, "raw.wav")
        wav_out = os.path.join(self.tmp, "fast.wav")
        gtd.convert_mp3_to_wav(self.mp3_path, wav_in)
        ok = gtd.apply_speed_and_noise(wav_in, wav_out, speed=1.35)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(wav_out))

    def test_trim_silence(self):
        wav_in = os.path.join(self.tmp, "raw.wav")
        wav_out = os.path.join(self.tmp, "trimmed.wav")
        gtd.convert_mp3_to_wav(self.mp3_path, wav_in)
        ok = gtd.trim_silence(wav_in, wav_out)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(wav_out))
        # Trimmed should be smaller or similar
        self.assertLessEqual(os.path.getsize(wav_out), os.path.getsize(wav_in) * 1.1)


class TestFullPipelineSample(unittest.TestCase):
    """End-to-end: generate_sample() produces valid 16kHz mono WAV."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def test_generate_sample_positive(self):
        out_path = os.path.join(self.out_dir, "claudette_test.wav")
        variant = {"lang": "en", "tld": "com", "label": "en-us"}
        ok = gtd.generate_sample(
            phrase="Claudette",
            variant=variant,
            speed=1.0,
            noise_amp=0.0,
            out_path=out_path,
            tmp_dir=self.tmp,
        )
        self.assertTrue(ok, "generate_sample returned False")
        self.assertTrue(os.path.exists(out_path), "Output file not created")
        info = wav_info(out_path)
        self.assertEqual(info.get("sample_rate"), 16000)
        self.assertEqual(info.get("channels"), 1)

    def test_generate_sample_with_speed(self):
        out_path = os.path.join(self.out_dir, "claudette_fast.wav")
        variant = {"lang": "fr", "tld": "fr", "label": "fr"}
        ok = gtd.generate_sample(
            phrase="Hey Claudette",
            variant=variant,
            speed=1.2,
            noise_amp=0.005,
            out_path=out_path,
            tmp_dir=self.tmp,
        )
        self.assertTrue(ok)
        info = wav_info(out_path)
        self.assertEqual(info.get("sample_rate"), 16000)


class TestManifest(unittest.TestCase):
    """Verify manifest.json is written correctly."""

    def test_manifest_written_after_generate(self):
        with tempfile.TemporaryDirectory() as out_dir:
            # Generate just 2 samples each (fast)
            import unittest.mock as mock
            # Stub generate_sample to avoid real network calls
            with mock.patch.object(gtd, "generate_sample", return_value=True) as mock_gen:
                # Also stub os.path.exists for output files
                with mock.patch("os.path.exists", return_value=True):
                    with mock.patch("os.path.getsize", return_value=5000):
                        gtd.generate_dataset(out_dir, target_positive=2, target_negative=2, verbose=False)

            manifest_path = os.path.join(out_dir, "manifest.json")
            self.assertTrue(os.path.exists(manifest_path), "manifest.json not written")
            with open(manifest_path) as f:
                data = json.load(f)
            self.assertIn("stats", data)
            self.assertIn("generated", data)
            self.assertIn("notes", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
