# Tasks — wipe-on-uninstall-verification

Component: `brain/`. Each task ≤ 2 hours. Verification step is named per task.

- [ ] **1. Land `brain/sealed_data_audit.md`** — the canonical pipe-table of
  every persistent path the stack writes to, classified `wipe` / `keep` /
  `oos`, sourced from the design doc. _Verify:_ table parses cleanly when fed
  to the verifier in step 3.
- [ ] **2. Implement `brain/uninstall_claudette.sh`** — idempotent removal of
  `wipe`-classified paths only, with `--root`, `--include-ha`,
  `--include-repo`, `--include-models`, `--yes` flags, surgical
  `/etc/environment` line removal, and systemd stop+disable+unlink for the
  two unit files. _Verify:_ run twice under a sandbox `--root`; second run is
  a no-op exit 0.
- [ ] **3. Implement `brain/wipe_uninstall_verifier.py`** — parses the audit
  table, supports `--report` / `--verify` / `--full --root` modes, stdlib
  only. _Verify:_ run `--report` against the live tree on the workshop and
  compare visually to the audit table.
- [ ] **4. Add `brain/test_wipe_uninstall_verifier.py`** — real end-to-end
  test against `tmp_path` covering the round-trip, idempotency, and the
  residue-failure negative case. _Verify:_ `python3 -m pytest
  brain/test_wipe_uninstall_verifier.py -v` passes locally.
- [ ] **5. Run the full brain test suite** to make sure the new test doesn't
  break or get broken by anything in the existing suite. _Verify:_ `python3
  -m pytest brain/ -v` is green (or honestly skipped where it already was).
- [ ] **6. Commit with `CLH-25` reference and `[symphony:done]` in the body**
  so the harness closes the issue. _Verify:_ `git log -1` shows both markers.
