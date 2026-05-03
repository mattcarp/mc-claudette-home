# Design — wipe-on-uninstall verification

## Sealed-data inventory (initial)

Sourced from grepping the repo for `/etc/`, `Path.home()`, `/tmp`, `tempfile`,
`HA_TOKEN`, etc. Each path is classified `wipe`, `keep`, or `oos` (out of
scope).

| Path                                                  | Owner                | Class | Notes                                                                          |
| ----------------------------------------------------- | -------------------- | ----- | ------------------------------------------------------------------------------ |
| `/etc/environment` (lines `HA_TOKEN`, `HA_URL`, `PORCUPINE_ACCESS_KEY`) | install scripts     | wipe  | Surgical line removal, not file deletion. Other env vars must survive.         |
| `/etc/systemd/system/claudette-pipeline.service`      | `voice/pipeline.py`  | wipe  | Stop + disable before unlink.                                                  |
| `/etc/systemd/system/claudette-wake-word.service`     | `voice/wake_word/`   | wipe  | Same — stop + disable + unlink.                                                |
| `~/.openclaw/openclaw.json`                           | `voice/conversation_fallback.py:60` | wipe  | OpenClaw API config Claudette wrote.                                |
| `/tmp/claudette-tts/`                                 | `voice/tts_responder.py:80`         | wipe  | Spoken-response audio cache.                                        |
| `/tmp/bu-*.{sock,pid,log}`                            | unrelated (browser-harness) | oos | Not ours; do not touch.                                                |
| `~/homeassistant/`                                    | `ha/setup_ha_docker.sh:29`          | keep  | Operator-owned. Removed only with `--include-ha`.                   |
| Docker container `homeassistant` + named volume       | `ha/setup_ha_docker.sh`             | keep  | Same — removed only with `--include-ha`.                            |
| `~/mc-claudette-home/` (cloned repo)                  | operator             | keep  | Removed only with `--include-repo`.                                            |
| `~/.cache/whisper/` model weights                     | Whisper STT          | keep  | Multi-GB model cache shared with anything else using Whisper. `--include-models` to opt in. |
| Porcupine model files under repo `voice/wake_word/`   | repo                 | keep  | Travel with the repo; covered by `--include-repo`.                             |
| systemd journal entries                               | system               | oos   | Documented as out-of-scope; operator can `journalctl --vacuum-time` themselves.|
| Docker image layers (`ghcr.io/home-assistant/...`)    | shared docker        | oos   | Removing affects unrelated workloads.                                          |

Every entry above lives in `brain/sealed_data_audit.md` as the canonical copy.
Adding a new persistent path **must** be accompanied by a row in that table —
enforced by the verifier's `--verify` mode reading the table and asserting it
matches the on-disk reality.

## Uninstall script behaviour

`brain/uninstall_claudette.sh`:

- Always idempotent — second run is a no-op and exits 0.
- Removes only paths classified `wipe`.
- For `/etc/environment`, deletes only the three known lines via `sed -i
  '/^HA_TOKEN=/d'` etc. — never the whole file.
- For systemd units: `systemctl stop && systemctl disable && rm` then
  `systemctl daemon-reload`.
- Refuses to run as a non-root user when `wipe` paths under `/etc` exist
  (clear error, exit code 2).
- Optional flags `--include-ha`, `--include-repo`, `--include-models` opt into
  the `keep` paths; the script confirms each with a `read -r -p` prompt unless
  `--yes` is also passed.
- Honours `--root <prefix>` to relocate every absolute path under a sandbox
  root — used by the test suite and by the `--full` verifier mode.

## Verifier behaviour

`brain/wipe_uninstall_verifier.py`:

- Single source of truth: parses `brain/sealed_data_audit.md`'s pipe table.
  This means the audit doc and the verifier can never drift — if you add a
  path to the table, the verifier picks it up; if you forget the table, the
  test fails.
- Resolves each path against an optional `--root` prefix.
- For `/etc/environment`, "absent" means **the named env var line is missing**,
  not that the file is gone.
- Modes:
  - `--report` — prints a table to stdout with status (`present` / `absent` /
    `n/a`) and class. Always exit 0 (informational).
  - `--verify` — fails with non-zero exit if any `wipe` row is `present`. Names
    each offender, one per line, on stderr.
  - `--full` — requires `--root <tmp>`; populates each `wipe` path with a
    sentinel byte, runs the uninstall script under that root, then re-runs
    `--verify`. Exits 0 only on full round-trip success.
- Strictly stdlib (`argparse`, `pathlib`, `subprocess`, `re`). No third-party
  deps; this script must run on the bare appliance.

## Test strategy (real, no mocks)

`brain/test_wipe_uninstall_verifier.py` exercises the verifier end-to-end:

1. `pytest`'s `tmp_path` fixture is the sandbox root.
2. The test plants real files / dirs at every `wipe` path under that root,
   plus a fake `/etc/environment` with the three sealed lines plus a couple of
   unrelated lines that must survive.
3. Calls `wipe_uninstall_verifier.py --full --root <tmp_path>` as a subprocess
   (no in-process import) so we exercise the same code path operators run.
4. Asserts: exit code 0; each sealed `wipe` path is gone; the unrelated
   `/etc/environment` lines remain; `keep` paths are untouched; running the
   verifier a second time still exits 0 (idempotency).
5. Negative test: pre-stage a file at a `wipe` path, **skip** the uninstall
   step, run `--verify` only, assert non-zero exit and that the offending path
   appears on stderr.

There is no mock of the filesystem, no mock of the audit doc, no mock of
subprocess — the test exercises the same script the operator runs.

## Failure modes the design accepts

- **Operator runs uninstall without root and `wipe` paths under `/etc` exist.**
  Script exits 2 with a clear "needs root" message. Verifier still works for
  user-owned paths.
- **Audit table out of sync with reality.** Verifier flags it: any `wipe` row
  that resolves to a path under a parent the script doesn't recognise raises
  in `--verify`.
- **`/tmp` is a tmpfs cleared at boot.** Acceptable — verifier still passes
  because the path is absent. Doc mentions it.

## What this design intentionally does not do

- Does **not** import the verifier into the test (we run it as a subprocess so
  CLI surface is part of the test).
- Does **not** scan the entire disk for residue. The audit table is the
  contract; if the stack starts writing somewhere new, that's a code-review
  fix in the audit table, not a probabilistic scan.
- Does **not** ship a GUI. Mattie runs it from a panel SSH session.
