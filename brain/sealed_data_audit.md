# Claudette Home — Sealed-Data Audit

**Canonical inventory of every persistent path the Claudette stack writes to.**
This file is the contract: `wipe_uninstall_verifier.py` parses the table below
and treats it as the source of truth. Any new path the stack persists **must**
be added here, or the verifier and its test will fail.

## Classification

- **wipe** — Claudette put it there. The uninstaller removes it.
- **keep** — Operator-owned or shared with non-Claudette workloads. Preserved
  by default; opt-in via uninstaller flag.
- **oos** — Out of scope. Not Claudette's to manage (kernel logs, shared
  Docker layers, browser-harness sockets, etc.).

## Path table

<!-- BEGIN: SEALED-DATA-TABLE -->

| Path | Class | Owner | Notes |
| --- | --- | --- | --- |
| /etc/environment#HA_TOKEN | wipe | ha/setup_ha_docker.sh | Surgical line removal — never delete the file. |
| /etc/environment#HA_URL | wipe | ha/setup_ha_docker.sh | Surgical line removal. |
| /etc/environment#PORCUPINE_ACCESS_KEY | wipe | voice/wake_word/setup_porcupine.py | Surgical line removal. |
| /etc/systemd/system/claudette-pipeline.service | wipe | voice/pipeline.py | Stop + disable + unlink + daemon-reload. |
| /etc/systemd/system/claudette-wake-word.service | wipe | voice/wake_word/ | Stop + disable + unlink + daemon-reload. |
| ~/.openclaw/openclaw.json | wipe | voice/conversation_fallback.py | OpenClaw config Claudette wrote. |
| /tmp/claudette-tts | wipe | voice/tts_responder.py | TTS audio cache directory. |
| ~/homeassistant | keep | ha/setup_ha_docker.sh | Operator-owned HA config. Opt-in via --include-ha. |
| ~/.cache/whisper | keep | Whisper STT | Multi-GB shared model cache. Opt-in via --include-models. |
| ~/mc-claudette-home | keep | operator | Cloned repo. Opt-in via --include-repo. |

<!-- END: SEALED-DATA-TABLE -->

## Notes per row

### `/etc/environment` (three lines)

`ha/setup_ha_docker.sh` writes `HA_TOKEN=` and `HA_URL=` lines to
`/etc/environment` on first install. `voice/wake_word/setup_porcupine.py`
documents writing `PORCUPINE_ACCESS_KEY=` to the same file. The uninstaller
deletes only those three lines via `sed -i '/^HA_TOKEN=/d'` (and equivalents);
unrelated entries written by the operator survive.

### `~/.openclaw/openclaw.json`

`voice/conversation_fallback.py:60` reads `Path.home() / ".openclaw" /
"openclaw.json"`. Claudette writes it; we delete the file and the directory if
the directory is then empty. If the operator put other files there we leave
the directory in place.

### `/tmp/claudette-tts/`

`voice/tts_responder.py:80` defaults the TTS audio cache to
`/tmp/claudette-tts`. Cleared on uninstall and naturally gone on reboot
(tmpfs).

### `~/homeassistant/` and the Docker container — `keep` by default

The HA config dir at `~/homeassistant/` (`ha/setup_ha_docker.sh:29`) and the
`homeassistant` Docker container are operator data. Pulling Claudette out of
the appliance does **not** touch them unless the operator passes
`--include-ha` to the uninstaller (and confirms the prompt unless `--yes`).

### `~/.cache/whisper/`

Whisper model weights are multi-GB and may be shared with non-Claudette
tooling. Default `keep`; `--include-models` opts in.

## Out of scope (`oos`) — explicitly not touched

These are deliberately not enumerated as table rows because they are not
Claudette's to delete:

- **systemd journal entries.** Operator can `journalctl --vacuum-time=1d` if
  they want; we do not.
- **Docker image layers** (`ghcr.io/home-assistant/...`). Removing them
  affects unrelated workloads.
- **`/tmp/bu-*.{sock,pid,log}`** — owned by the unrelated `browser-harness`
  daemon. We do not touch them.
- **Android panel-side state** (browser cache, Android user data). Covered by
  a separate audit; this doc scopes the brain host only.

## How to extend

Adding a new persistent path:

1. Add a row to the table above with the correct class.
2. If `wipe`, teach `brain/uninstall_claudette.sh` how to remove it.
3. Run `python3 -m pytest brain/test_wipe_uninstall_verifier.py -v`.

The verifier parses this table by looking between the `BEGIN: SEALED-DATA-TABLE`
and `END: SEALED-DATA-TABLE` HTML comments and reading the markdown pipe table
between them. Don't move those markers.
