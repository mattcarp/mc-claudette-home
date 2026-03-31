#!/usr/bin/env bash
# =============================================================================
# Claudette Home — MOES Panel Day-1 Setup Script
# Run this on the Workshop when the MOES 10.1" panel arrives.
#
# What it does:
#   1. Verifies the full Workshop pipeline stack is healthy
#   2. Installs alsa-utils if missing (for audio device detection)
#   3. Prints the Android/browser URL for the dashboard
#   4. Guides you through the 3-step Porcupine setup if not yet done
#   5. Validates the claudette-stt.service + wake word bridge
#   6. Runs the full panel_readiness.py preflight
#   7. Prints the day-1 quick-start checklist for the panel
#
# Usage:
#   chmod +x voice/setup_panel_day1.sh
#   bash voice/setup_panel_day1.sh
#
# Run from mc-home project root.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN="\033[92m"
RED="\033[91m"
YELLOW="\033[93m"
CYAN="\033[96m"
BOLD="\033[1m"
RESET="\033[0m"

OK="${GREEN}✅${RESET}"
FAIL="${RED}❌${RESET}"
WARN="${YELLOW}⚠️ ${RESET}"
INFO="${CYAN}ℹ️ ${RESET}"

header() {
    echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  $1${RESET}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════${RESET}"
}

step() {
    echo -e "\n${BOLD}  ── $1 ──${RESET}"
}

pass() { echo -e "  $OK $1"; }
fail() { echo -e "  $FAIL $1"; }
warn() { echo -e "  $WARN $1"; }
info() { echo -e "  $INFO $1"; }

# =============================================================================
header "Claudette Home — Panel Day-1 Setup"
echo -e "  $(date -u '+%Y-%m-%d %H:%M UTC')"
echo -e "  Workshop: $(hostname) / $(uname -m)"
# =============================================================================

# ── 1. Workshop stack health ──────────────────────────────────────────────────
step "1. Workshop Stack Health"

if systemctl is-active claudette-stt.service >/dev/null 2>&1; then
    BACKEND=$(curl -s http://127.0.0.1:8765/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('backend','?'))" 2>/dev/null || echo "unknown")
    pass "claudette-stt.service active (backend: $BACKEND)"
else
    fail "claudette-stt.service NOT running"
    echo -e "       Fix: sudo systemctl start claudette-stt.service"
fi

# Check STT latency — use /health endpoint (no audio needed)
LATENCY=$(python3 - <<'EOF' 2>/dev/null
import time, urllib.request, json
t0 = time.time()
try:
    with urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=10) as r:
        ms = int((time.time() - t0) * 1000)
        data = json.loads(r.read())
        if data.get("status") == "ok":
            print(ms)
        else:
            print(f"ERROR: {data}")
except Exception as e:
    print(f"ERROR: {e}")
EOF
)

if [[ "$LATENCY" =~ ^[0-9]+$ ]]; then
    if (( LATENCY < 3000 )); then
        pass "STT latency ${LATENCY}ms (target <3000ms)"
    else
        warn "STT latency ${LATENCY}ms — slow! Check Workshop load"
    fi
else
    fail "STT API not responding: $LATENCY"
fi

# ── 2. Install missing system deps ───────────────────────────────────────────
step "2. System Dependencies"

if command -v arecord >/dev/null 2>&1; then
    pass "alsa-utils installed (arecord available)"
else
    warn "alsa-utils not installed — installing..."
    if sudo apt-get install -y alsa-utils >/dev/null 2>&1; then
        pass "alsa-utils installed"
    else
        fail "Could not install alsa-utils (sudo may not be available)"
    fi
fi

if command -v ffplay >/dev/null 2>&1; then
    pass "ffplay available (audio playback)"
else
    warn "ffplay not found — installing ffmpeg..."
    if sudo apt-get install -y ffmpeg >/dev/null 2>&1; then
        pass "ffmpeg installed"
    else
        fail "Could not install ffmpeg"
    fi
fi

# ── 3. Porcupine credentials check ───────────────────────────────────────────
step "3. Porcupine Wake Word (Critical)"

PORC_KEY="${PORCUPINE_ACCESS_KEY:-${PICOVOICE_ACCESS_KEY:-}}"
MODEL_PATH="$SCRIPT_DIR/wake_word/models/claudette_linux.ppn"

if [[ -n "$PORC_KEY" ]]; then
    pass "PORCUPINE_ACCESS_KEY is set"
else
    fail "PORCUPINE_ACCESS_KEY not set"
    echo ""
    echo -e "  ${BOLD}${YELLOW}⚡ ACTION REQUIRED (2 minutes):${RESET}"
    echo -e "  1. Open: https://console.picovoice.ai"
    echo -e "  2. Sign up / log in (free tier)"
    echo -e "  3. Copy your Access Key from the dashboard"
    echo -e "  4. Run:"
    echo -e "     ${CYAN}infisical secrets set PORCUPINE_ACCESS_KEY <your-key>${RESET}"
    echo -e "     ${CYAN}echo 'PORCUPINE_ACCESS_KEY=<your-key>' | sudo tee -a /etc/environment${RESET}"
    echo -e "     ${CYAN}source /etc/environment${RESET}"
    echo ""
fi

if [[ -f "$MODEL_PATH" ]]; then
    SIZE=$(stat -c%s "$MODEL_PATH" 2>/dev/null || echo 0)
    pass "claudette_linux.ppn present (${SIZE} bytes)"
else
    fail "claudette_linux.ppn missing"
    echo ""
    echo -e "  ${BOLD}${YELLOW}⚡ ACTION REQUIRED (5 minutes):${RESET}"
    echo -e "  1. Log in at https://console.picovoice.ai"
    echo -e "  2. Go to: Wake Word → Train a custom wake word"
    echo -e "  3. Wake word: ${BOLD}claudette${RESET}"
    echo -e "  4. Platform: ${BOLD}Linux (x86_64)${RESET}"
    echo -e "  5. Download the .ppn file"
    echo -e "  6. Place it at:"
    echo -e "     ${CYAN}$MODEL_PATH${RESET}"
    echo ""
fi

# ── 4. Panel browser URL ──────────────────────────────────────────────────────
step "4. Panel Browser URL"

TAILSCALE_IP="100.118.26.94"
DASHBOARD_URL="http://${TAILSCALE_IP}/dashboard/"
FLOOR_PLAN_URL="http://${TAILSCALE_IP}/dashboard/floor-plan.html"

info "Dashboard URL (enter on MOES panel browser):"
echo -e "     ${BOLD}${GREEN}http://mc-claudette.com/${RESET}"
echo -e "     (or via Tailscale IP: ${CYAN}${DASHBOARD_URL}${RESET})"
echo ""
info "Floor plan / device control:"
echo -e "     ${BOLD}${GREEN}http://mc-claudette.com/dashboard/floor-plan.html${RESET}"
echo ""

# ── 5. Full pre-flight check ──────────────────────────────────────────────────
step "5. Full Pre-Flight Check (panel_readiness.py)"

echo ""
cd "$PROJECT_ROOT"
python3 voice/panel_readiness.py || true

# ── 6. Day-1 Panel Quick-Start Checklist ─────────────────────────────────────
header "Day-1 Panel Quick-Start Checklist"

cat << 'CHECKLIST'

  When the MOES 10.1" panel arrives, do this in order:

  □ 1. PHYSICAL SETUP
       - Unbox panel, connect POE cable (Cat5e/Cat6)
       - Or: connect DC 12V-24V power adapter
       - The panel should boot to Android 11 home screen

  □ 2. WIFI (if not using POE)
       - Settings → WiFi → connect to Xagħra home network
       - Note the panel's IP address (Settings → About → IP)

  □ 3. BROWSER ON PANEL
       - Open Chrome/Chromium on the panel
       - Navigate to: http://mc-claudette.com/
       - Should show Mission Control dashboard
       - Navigate to: http://mc-claudette.com/dashboard/floor-plan.html
       - Should show interactive Xagħra floor plan

  □ 4. PORCUPINE WAKE WORD (if not done yet)
       - Complete step 3 above (picovoice.ai + .ppn file)
       - Then run: python3 voice/wake_word/setup_porcupine.py
       - All checks should be green ✅

  □ 5. TEST WAKE WORD
       - Start the listener:
         python3 voice/wake_word/porcupine_listener.py
       - Say "Claudette" clearly in the room
       - Should print: ✅ Wake word detected!

  □ 6. TEST FULL PIPELINE (text mode, no hardware)
       - python3 voice/pipeline.py --text "turn on the living room lights" --stub
       - Should log: intent parsed → HA action (stub) → pipeline_response

  □ 7. TEST STT
       - Send a WAV to the STT API:
         curl -s -X POST http://127.0.0.1:8765/transcribe \
              -H "Content-Type: audio/wav" \
              --data-binary @/path/to/sample.wav
       - Should return: {"text": "...", "backend": "faster-whisper", ...}

  □ 8. HOME ASSISTANT SETUP (if not done)
       - See: ha/README.md
       - HA should be running at http://localhost:8123
       - Issue #12: HA setup + config

  □ 9. CELEBRATE 🎉
       - Say "Claudette" → lights respond
       - You're live.

CHECKLIST

# ── 7. Summary ────────────────────────────────────────────────────────────────
header "Summary"

PORC_DONE=false
MODEL_DONE=false
[[ -n "$PORC_KEY" ]] && PORC_DONE=true
[[ -f "$MODEL_PATH" ]] && MODEL_DONE=true

if $PORC_DONE && $MODEL_DONE; then
    echo -e "  ${GREEN}${BOLD}🟢 PANEL-READY!${RESET} All critical checks passed."
    echo -e "     Plug in the MOES panel and run the Day-1 checklist above."
else
    echo -e "  ${YELLOW}${BOLD}🟡 ALMOST READY.${RESET} 1-2 manual steps needed:"
    if ! $PORC_DONE; then
        echo -e "     ${RED}→ Set PORCUPINE_ACCESS_KEY (picovoice.ai, 2 min)${RESET}"
    fi
    if ! $MODEL_DONE; then
        echo -e "     ${RED}→ Download claudette_linux.ppn (picovoice.ai, 5 min)${RESET}"
    fi
    echo ""
    echo -e "  Everything else is ready. Once Mattie does those 2 steps,"
    echo -e "  run this script again — it should be fully green."
fi

echo ""
echo -e "  ${CYAN}Re-run pre-flight anytime:${RESET}"
echo -e "     python3 voice/panel_readiness.py"
echo ""
