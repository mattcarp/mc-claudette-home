#!/usr/bin/env bash
# =============================================================================
# Claudette Home — Uninstaller (CLH-25)
#
# Removes only paths the Claudette stack put on this host. See
# brain/sealed_data_audit.md for the contract.
#
# Usage:
#   bash brain/uninstall_claudette.sh                     # wipe-class only
#   bash brain/uninstall_claudette.sh --include-ha        # also nuke HA config + container
#   bash brain/uninstall_claudette.sh --include-models    # also drop ~/.cache/whisper
#   bash brain/uninstall_claudette.sh --include-repo      # also remove the repo clone
#   bash brain/uninstall_claudette.sh --yes               # skip confirmation prompts
#   bash brain/uninstall_claudette.sh --root /tmp/sandbox # relocate every path under a sandbox
#
# Idempotent. Second invocation must exit 0 with no destructive ops.
# =============================================================================
set -euo pipefail

ROOT_PREFIX=""
INCLUDE_HA=0
INCLUDE_MODELS=0
INCLUDE_REPO=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)             ROOT_PREFIX="${2%/}"; shift 2 ;;
        --root=*)           ROOT_PREFIX="${1#*=}"; ROOT_PREFIX="${ROOT_PREFIX%/}"; shift ;;
        --include-ha)       INCLUDE_HA=1; shift ;;
        --include-models)   INCLUDE_MODELS=1; shift ;;
        --include-repo)     INCLUDE_REPO=1; shift ;;
        --yes|-y)           ASSUME_YES=1; shift ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "unknown flag: $1" >&2
            exit 2
            ;;
    esac
done

# Resolve a logical path to its on-disk location, honouring --root.
resolve() {
    local p="$1"
    # Expand ~ relative to the invoking user's HOME.
    p="${p/#\~/$HOME}"
    if [[ -n "$ROOT_PREFIX" ]]; then
        # /etc/foo -> $ROOT_PREFIX/etc/foo, /home/x -> $ROOT_PREFIX/home/x
        printf '%s%s' "$ROOT_PREFIX" "$p"
    else
        printf '%s' "$p"
    fi
}

confirm() {
    local prompt="$1"
    if [[ "$ASSUME_YES" -eq 1 ]]; then return 0; fi
    read -r -p "$prompt [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

# Surgical line removal in /etc/environment. Leaves unrelated lines alone.
# When ROOT_PREFIX is set we operate on the sandboxed copy; otherwise we
# require root for /etc writes.
strip_env_line() {
    local var="$1"
    local file
    file="$(resolve /etc/environment)"
    if [[ ! -f "$file" ]]; then return 0; fi

    if [[ -z "$ROOT_PREFIX" && "$EUID" -ne 0 ]]; then
        if grep -qE "^(export[[:space:]]+)?${var}=" "$file" 2>/dev/null; then
            echo "  needs root to strip ${var} from $file" >&2
            exit 2
        fi
        return 0
    fi

    # Use a temp file so we never truncate on partial sed failure.
    local tmp
    tmp="$(mktemp)"
    grep -vE "^(export[[:space:]]+)?${var}=" "$file" > "$tmp" || true
    # Preserve original mode/owner where possible; fall back silently.
    if [[ -z "$ROOT_PREFIX" ]]; then
        cat "$tmp" > "$file"
    else
        mv "$tmp" "$file"
    fi
    rm -f "$tmp"
    echo "  stripped ${var} from $file"
}

# Stop + disable a systemd unit if it exists, then unlink the file.
remove_unit() {
    local unit="$1"
    local file
    file="$(resolve "/etc/systemd/system/${unit}")"

    if [[ -z "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null 2>&1; then
        if systemctl list-unit-files "${unit}" 2>/dev/null | grep -q "${unit}"; then
            systemctl stop "${unit}" 2>/dev/null || true
            systemctl disable "${unit}" 2>/dev/null || true
        fi
    fi

    if [[ -e "$file" ]]; then
        if [[ -z "$ROOT_PREFIX" && "$EUID" -ne 0 ]]; then
            echo "  needs root to remove $file" >&2
            exit 2
        fi
        rm -f "$file"
        echo "  removed $file"
    fi

    if [[ -z "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload 2>/dev/null || true
    fi
}

# Remove a path (file or directory). No-op if absent.
remove_path() {
    local path
    path="$(resolve "$1")"
    if [[ -e "$path" || -L "$path" ]]; then
        rm -rf "$path"
        echo "  removed $path"
    fi
}

# Remove a directory only if it ends up empty after we delete the named file
# inside it. Used for ~/.openclaw/ — we own openclaw.json but not the dir
# itself if the operator put other things in it.
remove_file_then_empty_parent() {
    local file
    file="$(resolve "$1")"
    if [[ -e "$file" ]]; then
        rm -f "$file"
        echo "  removed $file"
    fi
    local parent
    parent="$(dirname "$file")"
    if [[ -d "$parent" ]] && [[ -z "$(ls -A "$parent" 2>/dev/null)" ]]; then
        rmdir "$parent"
        echo "  removed empty parent $parent"
    fi
}

echo "Claudette uninstall starting (root=${ROOT_PREFIX:-/})"

# --- wipe-class paths ---------------------------------------------------------
strip_env_line HA_TOKEN
strip_env_line HA_URL
strip_env_line PORCUPINE_ACCESS_KEY

remove_unit claudette-pipeline.service
remove_unit claudette-wake-word.service

remove_file_then_empty_parent "~/.openclaw/openclaw.json"
remove_path "/tmp/claudette-tts"

# --- opt-in keep-class paths --------------------------------------------------
if [[ "$INCLUDE_HA" -eq 1 ]]; then
    if confirm "Wipe ~/homeassistant/ and the 'homeassistant' Docker container?"; then
        remove_path "~/homeassistant"
        if [[ -z "$ROOT_PREFIX" ]] && command -v docker >/dev/null 2>&1; then
            if docker inspect homeassistant >/dev/null 2>&1; then
                docker rm -f homeassistant >/dev/null 2>&1 || true
                echo "  removed docker container homeassistant"
            fi
        fi
    fi
fi

if [[ "$INCLUDE_MODELS" -eq 1 ]]; then
    if confirm "Wipe ~/.cache/whisper/ (multi-GB model cache)?"; then
        remove_path "~/.cache/whisper"
    fi
fi

if [[ "$INCLUDE_REPO" -eq 1 ]]; then
    if confirm "Wipe ~/mc-claudette-home/ (the cloned repo)?"; then
        remove_path "~/mc-claudette-home"
    fi
fi

echo "Claudette uninstall complete."
