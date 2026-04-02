#!/usr/bin/env bash
# =============================================================================
# Claudette Home — Home Assistant Docker Installer
# Run this on the Workshop to install HA and generate a long-lived token.
#
# What it does:
#   1. Checks Docker is installed and running
#   2. Pulls + starts the HA container (stable, port 8123)
#   3. Waits for HA to become healthy (~90s first boot)
#   4. Walks you through onboarding (opens URL in browser if on desktop)
#   5. Reminds you to generate a long-lived token and set HA_TOKEN
#   6. Runs a quick smoke-test: curl the API with the token
#   7. Verifies ha_bridge.py can ping HA
#
# Usage:
#   chmod +x ha/setup_ha_docker.sh
#   bash ha/setup_ha_docker.sh
#
# After this script succeeds:
#   - HA is running at http://localhost:8123
#   - HA_TOKEN is set in /etc/environment
#   - ha_bridge.py --action ping works
#
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
HA_CONFIG_DIR="/home/sysop/homeassistant"
HA_URL="http://localhost:8123"
HA_CONTAINER="homeassistant"

GREEN="\033[92m"; RED="\033[91m"; YELLOW="\033[93m"; CYAN="\033[96m"; BOLD="\033[1m"; RESET="\033[0m"
OK="${GREEN}✅${RESET}"; FAIL="${RED}❌${RESET}"; WARN="${YELLOW}⚠️ ${RESET}"; INFO="${CYAN}ℹ️ ${RESET}"

header() {
    echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  $1${RESET}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════${RESET}"
}
pass() { echo -e "  $OK $1"; }
fail() { echo -e "  $FAIL $1"; exit 1; }
warn() { echo -e "  $WARN $1"; }
info() { echo -e "  $INFO $1"; }

header "Claudette Home — Home Assistant Docker Setup"
echo -e "  $(date -u '+%Y-%m-%d %H:%M UTC')"
echo -e "  Workshop: $(hostname)"

# ── 1. Docker check ────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ── 1. Docker Check ──${RESET}"
if ! command -v docker &>/dev/null; then
    fail "Docker not installed. Install it first: https://docs.docker.com/engine/install/ubuntu/"
fi
if ! docker info &>/dev/null 2>&1; then
    fail "Docker daemon not running. Run: sudo systemctl start docker"
fi
pass "Docker installed and running ($(docker --version | head -1))"

# ── 2. Create config dir ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ── 2. Config Directory ──${RESET}"
mkdir -p "$HA_CONFIG_DIR"
pass "HA config dir: $HA_CONFIG_DIR"

# ── 3. Container — start or verify ────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ── 3. Home Assistant Container ──${RESET}"

if docker inspect "$HA_CONTAINER" &>/dev/null 2>&1; then
    CONTAINER_STATUS=$(docker inspect -f '{{.State.Status}}' "$HA_CONTAINER" 2>/dev/null || echo "unknown")
    if [[ "$CONTAINER_STATUS" == "running" ]]; then
        pass "Container '$HA_CONTAINER' already running — skipping pull"
    else
        warn "Container '$HA_CONTAINER' exists but not running (status: $CONTAINER_STATUS). Starting..."
        docker start "$HA_CONTAINER"
        pass "Container started"
    fi
else
    info "Pulling ghcr.io/home-assistant/home-assistant:stable ..."
    docker pull ghcr.io/home-assistant/home-assistant:stable
    info "Starting HA container..."
    docker run -d \
        --name "$HA_CONTAINER" \
        --privileged \
        --restart unless-stopped \
        -v "$HA_CONFIG_DIR:/config" \
        -p 8123:8123 \
        --network host \
        ghcr.io/home-assistant/home-assistant:stable
    pass "Container created and started"
fi

# ── 4. Wait for HA to become healthy ──────────────────────────────────────────
echo ""
echo -e "${BOLD}  ── 4. Waiting for HA to be Ready (up to 120s) ──${RESET}"
info "This takes ~60-90s on first boot..."

WAIT_MAX=120
WAIT_ELAPSED=0
WAIT_INTERVAL=5
HA_READY=0

while [[ $WAIT_ELAPSED -lt $WAIT_MAX ]]; do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${HA_URL}/api/" 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "401" ]]; then
        # 401 = HA running, just needs auth (which is what we expect without token)
        HA_READY=1
        break
    fi
    echo -n "."
    sleep "$WAIT_INTERVAL"
    WAIT_ELAPSED=$((WAIT_ELAPSED + WAIT_INTERVAL))
done
echo ""

if [[ "$HA_READY" -eq 0 ]]; then
    warn "HA not responding after ${WAIT_MAX}s. It may still be starting up."
    info "Check logs: docker logs homeassistant --tail 50"
    info "Try again in 30 seconds."
    fail "HA startup timeout"
fi
pass "Home Assistant is responding at $HA_URL"

# ── 5. Onboarding ─────────────────────────────────────────────────────────────
echo ""
header "Onboarding Required (Manual Step)"
echo -e "
  ${BOLD}Home Assistant needs one-time manual onboarding.${RESET}

  Open this URL in your browser:
  ${CYAN}  $HA_URL${RESET}

  Steps:
    1. Create a local admin account (e.g. admin / pick a password)
    2. Choose your location (Malta, UTC+1)
    3. Skip device discovery for now (or add later)
    4. Finish onboarding

  Once done, come back and press Enter to continue.
"
read -r -p "  Press Enter once you've completed onboarding... "

# ── 6. Long-lived token setup ─────────────────────────────────────────────────
echo ""
header "Generate Long-Lived Access Token"
echo -e "
  ${BOLD}You need a token so Claudette can talk to HA.${RESET}

  Steps:
    1. In HA, click your profile (bottom left) or go to:
       ${CYAN}$HA_URL/profile/security${RESET}
    2. Scroll down to 'Long-Lived Access Tokens'
    3. Click 'Create Token', name it 'claudette'
    4. ${BOLD}Copy the token — you only see it once!${RESET}
    5. Paste it below.
"
read -r -s -p "  Paste your HA token here (input hidden): " HA_TOKEN_INPUT
echo ""

if [[ -z "$HA_TOKEN_INPUT" ]]; then
    warn "No token entered — skipping token setup. Run again or set HA_TOKEN manually."
else
    # ── 7. Smoke-test the token ──────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}  ── 7. Verifying Token ──${RESET}"
    HA_API_RESPONSE=$(curl -sf \
        -H "Authorization: Bearer $HA_TOKEN_INPUT" \
        -H "Content-Type: application/json" \
        "${HA_URL}/api/" 2>/dev/null || echo "CURL_ERROR")

    if echo "$HA_API_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('message','').startswith('API running')" 2>/dev/null; then
        pass "Token is valid — HA API is authenticated"

        # ── 8. Write token to /etc/environment ──────────────────────────────
        echo ""
        echo -e "${BOLD}  ── 8. Persisting Token ──${RESET}"

        # Remove any existing HA_TOKEN line
        sudo sed -i '/^HA_TOKEN=/d' /etc/environment 2>/dev/null || true
        sudo sed -i '/^export HA_TOKEN=/d' /etc/environment 2>/dev/null || true

        # Write new token
        echo "HA_TOKEN=${HA_TOKEN_INPUT}" | sudo tee -a /etc/environment > /dev/null
        echo "HA_URL=http://localhost:8123" | sudo tee -a /etc/environment > /dev/null

        pass "HA_TOKEN saved to /etc/environment"
        info "Reload env: source /etc/environment"
        info "Or restart session to pick it up automatically"

        # Export for current shell test
        export HA_TOKEN="$HA_TOKEN_INPUT"
        export HA_URL="http://localhost:8123"

        # ── 9. ha_bridge.py smoke test ───────────────────────────────────────
        echo ""
        echo -e "${BOLD}  ── 9. ha_bridge.py Smoke Test ──${RESET}"
        cd "$PROJECT_ROOT"
        BRIDGE_RESULT=$(HA_TOKEN="$HA_TOKEN_INPUT" HA_URL="$HA_URL" \
            python3 voice/ha_bridge/ha_bridge.py --action ping 2>&1 || echo "BRIDGE_ERROR")
        if echo "$BRIDGE_RESULT" | grep -qi "pong\|ok\|connected\|running"; then
            pass "ha_bridge.py ping OK — Claudette can talk to HA"
        elif echo "$BRIDGE_RESULT" | grep -qi "error\|failed\|exception"; then
            warn "ha_bridge.py returned an error: $BRIDGE_RESULT"
            info "Check ANTHROPIC_API_KEY is set and voice/ha_bridge/ha_bridge.py is healthy"
        else
            info "ha_bridge.py output: $BRIDGE_RESULT"
            pass "ha_bridge.py ran without crashing"
        fi

        # ── 10. Entity count ─────────────────────────────────────────────────
        echo ""
        echo -e "${BOLD}  ── 10. HA Entity Count ──${RESET}"
        ENTITY_COUNT=$(HA_TOKEN="$HA_TOKEN_INPUT" HA_URL="$HA_URL" \
            python3 voice/ha_bridge/ha_bridge.py --action get_entities 2>/dev/null \
            | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data))" 2>/dev/null \
            || curl -sf \
                -H "Authorization: Bearer $HA_TOKEN_INPUT" \
                "${HA_URL}/api/states" 2>/dev/null \
                | python3 -c "import sys, json; print(len(json.load(sys.stdin)))" 2>/dev/null \
            || echo "unknown")
        pass "HA entities found: $ENTITY_COUNT"

    else
        warn "Token verification failed."
        info "Response: $HA_API_RESPONSE"
        info "Make sure you completed onboarding and pasted the full token."
        info "You can set it manually later:"
        echo ""
        echo -e "  ${CYAN}echo 'HA_TOKEN=<your-token>' | sudo tee -a /etc/environment${RESET}"
        echo -e "  ${CYAN}echo 'HA_URL=http://localhost:8123' | sudo tee -a /etc/environment${RESET}"
    fi
fi

# ── Final summary ─────────────────────────────────────────────────────────────
header "Setup Complete"
echo -e "
  ${BOLD}Home Assistant is running!${RESET}

  Dashboard:    ${CYAN}$HA_URL${RESET}
  Tailscale:    ${CYAN}http://100.118.26.94:8123${RESET}
  Config dir:   $HA_CONFIG_DIR
  Logs:         docker logs homeassistant --tail 50

  ${BOLD}Next steps:${RESET}
  1. Install WiiM integration in HA UI (Settings → Integrations → Add → LinkPlay)
  2. Install Alexa Media Player via HACS (see ha/integrations/alexa_media_player_setup.md)
  3. Add Aqara M3 hub when it arrives (Settings → Integrations → Add → Zigbee Home Automation)
  4. Run: ${CYAN}bash voice/setup_panel_day1.sh${RESET} to run full readiness check

  Panel arrives TODAY — once HA is configured, say 'Claudette' and watch the magic.
"
