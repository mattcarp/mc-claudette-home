#!/usr/bin/env bash
set -euo pipefail

echo "== Claudette Pi 5 audio check =="
echo

echo "-- Kernel / board --"
uname -a
printf '\n'

echo "-- ALSA playback devices --"
aplay -l || true
printf '\n'

echo "-- ALSA capture devices --"
arecord -l || true
printf '\n'

echo "-- Cards --"
cat /proc/asound/cards || true
printf '\n'

echo "-- Recording 3 seconds of test audio --"
TEST_WAV=/tmp/claudette-mic-test.wav
arecord -d 3 -f S16_LE -r 16000 "$TEST_WAV" || true
printf '\n'

if [ -f "$TEST_WAV" ]; then
  echo "-- File info --"
  file "$TEST_WAV" || true
  ls -lh "$TEST_WAV" || true
  printf '\n'
  echo "-- Playback test --"
  aplay "$TEST_WAV" || true
else
  echo "No test wav generated."
fi

printf '\n-- Boot config audio hints --\n'
if [ -f /boot/firmware/config.txt ]; then
  grep -Ei 'dtoverlay|dtparam=audio' /boot/firmware/config.txt || true
elif [ -f /boot/config.txt ]; then
  grep -Ei 'dtoverlay|dtparam=audio' /boot/config.txt || true
fi

echo
echo "Audio check complete."
