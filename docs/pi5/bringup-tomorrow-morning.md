# Claudette Pi 5 — Tomorrow Morning Bring-Up

**Target hardware**
- Raspberry Pi 5 16GB
- official/working USB-C power supply
- case
- RaspiAudio MIC ULTRA+
- RaspiAudio Audio+ V3 (optional, do not block bring-up)
- Aqara Hub M3 available on network/in hand

**Goal for first session**
Get to a working headless base system with:
1. SSH access
2. audio device visibility
3. Home Assistant container running
4. voice stack prerequisites installed
5. one verified mic/speaker loopback test

---

## Phase 0 — Flash + boot

Flash **Raspberry Pi OS Lite 64-bit**.

During imaging, preconfigure:
- hostname: `claudette`
- SSH enabled
- Wi-Fi if needed
- user account

After first boot, SSH in:

```bash
ssh <user>@claudette.local
```

Then run:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git curl jq ffmpeg alsa-utils python3-venv python3-pip
```

---

## Phase 1 — Pull repo + bootstrap

```bash
git clone https://github.com/mattcarp/mc-home.git ~/mc-home
cd ~/mc-home
bash scripts/pi5/bootstrap.sh
```

If cloning over SSH is preferred, use your normal GitHub path instead.

---

## Phase 2 — Audio bring-up (MIC ULTRA+ only)

Do **not** start with Audio+ V3 stacked.

1. Power down Pi
2. Mount **MIC ULTRA+ only**
3. Boot
4. Run:

```bash
cd ~/mc-home
bash scripts/pi5/check-audio.sh
```

Expected outcomes:
- ALSA playback device visible
- ALSA capture device visible
- loopback test recorded successfully

If not visible:
- reboot once
- re-seat HAT
- inspect `dmesg | tail -100`
- inspect `/boot/firmware/config.txt`

---

## Phase 3 — Home Assistant headless

```bash
cd ~/mc-home
bash ha/setup_ha_docker.sh
```

This should:
- verify Docker
- start Home Assistant container
- walk through onboarding
- help set `HA_TOKEN`

---

## Phase 4 — Voice readiness

```bash
cd ~/mc-home
python3 voice/panel_readiness.py
```

This is useful even on Pi 5 because it gives us a fast red/green readiness pass for:
- STT service assumptions
- Porcupine assumptions
- pipeline health
- env/config expectations

---

## Phase 5 — Product-specific notes

- **Audio+ V3** is follow-up work, not day-one work.
- If the MIC ULTRA+ works cleanly, we already have enough to start prototyping.
- If the Pi is stable by the end of the morning, next step is wake word + STT + TTS loop.

---

## First success definition

By the end of tomorrow morning, success is:
- Pi reachable over SSH
- repo cloned
- bootstrap complete
- MIC ULTRA+ visible to ALSA
- one record/playback loopback successful
- Home Assistant reachable on port 8123

Anything beyond that is bonus.
