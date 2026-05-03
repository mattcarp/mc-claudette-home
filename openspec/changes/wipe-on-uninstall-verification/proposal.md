# Proposal — wipe-on-uninstall verification + sealed-data audit

**Linear issue:** CLH-25
**Component:** `brain/` (with audit coverage of `voice/`, `ha/`, host filesystem)
**Source:** `docs/feature-brainstorm-2026-05-01.md` → "Wipe-on-uninstall verification" (*hop*)
**Cloud required?** No. Verification runs entirely on the local appliance.

## Why

We promise occupants that Claudette is **theirs** — "your hardware, your data, no
cloud dependency" (`README.md`). That promise is only as strong as what happens
when the system is removed. Today nothing in the repo describes, much less
verifies:

1. **What sealed data exists** while the brain is running (HA token in
   `/etc/environment`, the entire HA config directory at `~/homeassistant/`,
   `~/.openclaw/openclaw.json`, `/tmp/claudette-tts/`, Whisper / Porcupine model
   caches, systemd unit files under `/etc/systemd/system/`, the
   `homeassistant` Docker container + named volumes, the cloned repo).
2. **What gets wiped** on uninstall vs. left behind as "residue" (logs in the
   journal, `/tmp` audio fragments, HA snapshots, Docker layers, env vars in
   `/etc/environment`).
3. **How an operator (Mattie) can prove** to themselves — or to a guest who
   asks — that nothing personal stayed on the panel after the brain was
   uninstalled.

CLH-25 closes that gap with two artifacts: a written **sealed-data audit** (the
canonical list of every path the stack writes to) and a **verification script**
that uninstalls, re-installs on a clean host, and asserts the residue list is
empty.

## User-facing scenario

**Mattie at the Xagħra (Gozo) house.** A guest staying in the spare room asks,
"if you uninstall Claudette tonight, what's left of me on the panel?" Mattie
runs `python3 brain/wipe_uninstall_verifier.py --report` from the Pi 5 and
hands the guest a one-page report listing every path the stack ever touches,
each one marked **wiped**, **kept-by-design**, or **residue**. There are no
**residue** entries. The guest is satisfied; Mattie has the receipts.

**Same operator, post-deinstall.** Six months later, Mattie pulls Claudette out
of the guest room to redeploy the panel as a generic Home Assistant kiosk for
Rayes. He runs `bash brain/uninstall_claudette.sh && python3
brain/wipe_uninstall_verifier.py --verify`. The script exits non-zero if any
sealed path still exists, naming the file. He fixes the offender, re-runs,
gets a green pass, and hands the panel over.

## What changes

1. **New audit doc** `brain/sealed_data_audit.md` — the canonical, reviewed
   list of every persistent location the Claudette stack writes to, classified
   as `keep` (e.g. operator-owned HA config they want to retain), `wipe`
   (everything Claudette put there), or `out-of-scope` (kernel logs, Docker
   image cache shared with non-Claudette workloads).
2. **New uninstall script** `brain/uninstall_claudette.sh` — idempotent,
   removes only paths classified as `wipe` in the audit.
3. **New verifier** `brain/wipe_uninstall_verifier.py` with three modes:
   - `--report` prints the audit table with the live status of each path.
   - `--verify` exits 0 iff every `wipe` path is absent. Exits non-zero with a
     per-path reason otherwise.
   - `--full` runs the round-trip (`uninstall → verify → reinstall stub →
     uninstall → verify`) against a sandboxed root supplied via `--root` so it
     never touches the real `/etc` or `/home`.
4. **One real test** `brain/test_wipe_uninstall_verifier.py` exercising
   `--full` against a `tmp_path` root with synthesised sealed files. No mocks
   of the verifier itself — it runs end-to-end on a real filesystem.

## Non-goals

- **Not** a Home Assistant uninstaller. HA's own data is treated as
  operator-owned (`keep` in the audit) unless the operator passes
  `--include-ha` to the uninstall script. Same for the cloned repo.
- **Not** secure-erase / forensics-grade wiping. We unlink files and remove
  directories; we do not shred blocks. If an occupant has a forensic-recovery
  threat model they need disk-level encryption, which is a separate change.
- **Not** an attempt to verify residue on Android panels (YC-SM10P) in this
  change. The pilot's brain runs on the Pi 5 / workshop; panel-side residue
  (browser cache, Android user data) is its own audit.
- **Not** a privacy policy doc. The audit is the technical inventory; the
  user-facing privacy statement is downstream.
- **Not** a uninstall flow with rollback. One direction only.

## Risk if we don't ship this

Every future privacy claim ("no cloud dependency", "your data") becomes
unfalsifiable. The first guest, journalist, or compliance reviewer who asks
"prove it" gets hand-waving. CLH-25 makes the claim mechanically checkable.
