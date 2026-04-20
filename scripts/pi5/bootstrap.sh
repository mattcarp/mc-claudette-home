#!/usr/bin/env bash
set -euo pipefail

GREEN='\033[0;32m'
NC='\033[0m'

say() { printf "%b\n" "$1"; }

say "${GREEN}== Claudette Pi 5 bootstrap ==${NC}"

sudo apt update
sudo apt install -y \
  git curl jq ffmpeg alsa-utils docker.io docker-compose-v2 \
  python3 python3-venv python3-pip python3-dev \
  build-essential pkg-config libsndfile1

sudo systemctl enable docker
sudo systemctl start docker

mkdir -p .venvs

python3 -m venv .venvs/voice
source .venvs/voice/bin/activate
pip install --upgrade pip wheel

if [ -f voice/intent_parser/requirements.txt ]; then
  pip install -r voice/intent_parser/requirements.txt
fi
if [ -f voice/ha_bridge/requirements.txt ]; then
  pip install -r voice/ha_bridge/requirements.txt
fi
if [ -f voice/stt_pipeline/requirements.txt ]; then
  pip install -r voice/stt_pipeline/requirements.txt
fi

deactivate || true

say "${GREEN}Bootstrap complete.${NC}"
say "Next: bash scripts/pi5/check-audio.sh"
