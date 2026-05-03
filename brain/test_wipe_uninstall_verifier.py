"""
End-to-end test for the wipe-on-uninstall verifier (CLH-25).

No mocks: we plant real files / dirs under pytest's tmp_path, invoke the
verifier and the uninstall script as subprocesses (the same way operators
run them), and assert the round-trip.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFIER = REPO_ROOT / "brain" / "wipe_uninstall_verifier.py"
UNINSTALL_SCRIPT = REPO_ROOT / "brain" / "uninstall_claudette.sh"
AUDIT_DOC = REPO_ROOT / "brain" / "sealed_data_audit.md"

# Make the verifier module importable for the parser-only smoke check.
sys.path.insert(0, str(REPO_ROOT / "brain"))
import wipe_uninstall_verifier as wuv  # noqa: E402


def _run_verifier(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def _sandbox_home(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Build a sandbox HOME under tmp_path; return (home, env)."""
    home = tmp_path / "home" / "sysop"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    return home, env


def test_audit_doc_parses_and_has_known_classes():
    """The audit table must parse and contain at least one wipe row."""
    entries = wuv.parse_audit(AUDIT_DOC)
    assert entries, "audit doc has no entries"
    classes = {e.cls for e in entries}
    assert "wipe" in classes, "audit must have at least one wipe row"
    assert classes <= wuv.VALID_CLASSES, f"unknown classes: {classes - wuv.VALID_CLASSES}"


def test_full_round_trip_under_sandbox_root(tmp_path: Path):
    """Plant sentinels, run --full, expect a clean verify."""
    home, env = _sandbox_home(tmp_path)
    root = tmp_path / "sandbox"
    root.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--full",
            "--root",
            str(root),
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, (
        f"--full failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    # Final verify must say clean.
    assert "verify: clean" in proc.stdout

    # Belt-and-braces: every wipe-class row should now resolve to absent.
    entries = wuv.parse_audit(AUDIT_DOC)
    for entry in entries:
        if entry.cls != "wipe":
            continue
        target = entry.resolve(str(root), home)
        if entry.env_var is not None:
            if target.exists():
                content = target.read_text(encoding="utf-8")
                assert f"{entry.env_var}=" not in content, (
                    f"residue line for {entry.env_var} in {target}"
                )
        else:
            assert not target.exists(), f"residue path: {target}"


def test_unrelated_env_lines_survive(tmp_path: Path):
    """The uninstaller must do surgical line removal on /etc/environment."""
    home, env = _sandbox_home(tmp_path)
    root = tmp_path / "sandbox"
    root.mkdir()

    # Run --full so the planter writes the file with two unrelated lines plus
    # the three sealed lines, then the uninstaller strips the sealed ones.
    proc = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--full",
            "--root",
            str(root),
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr

    env_file = root / "etc" / "environment"
    assert env_file.exists(), f"env file should still exist: {env_file}"
    content = env_file.read_text(encoding="utf-8")
    assert "PATH=/usr/local/sbin:/usr/local/bin" in content
    assert "LANG=en_GB.UTF-8" in content
    for sealed in ("HA_TOKEN", "HA_URL", "PORCUPINE_ACCESS_KEY"):
        assert f"{sealed}=" not in content, f"sealed line {sealed} survived"


def test_verify_flags_residue_when_uninstall_skipped(tmp_path: Path):
    """If we plant a sentinel and then *skip* the uninstall, verify must fail."""
    home, env = _sandbox_home(tmp_path)
    root = tmp_path / "sandbox"
    root.mkdir()

    # Plant a single, unambiguous wipe-class file directly: openclaw.json.
    openclaw = root / str(home).lstrip("/") / ".openclaw" / "openclaw.json"
    openclaw.parent.mkdir(parents=True, exist_ok=True)
    openclaw.write_bytes(b"sentinel")

    proc = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--verify",
            "--root",
            str(root),
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode != 0, "verify should fail when residue exists"
    assert "openclaw.json" in proc.stderr, (
        f"stderr should name the offender, got: {proc.stderr!r}"
    )


def test_full_is_idempotent(tmp_path: Path):
    """Two consecutive --full runs both succeed; the second is a no-op."""
    home, env = _sandbox_home(tmp_path)
    root = tmp_path / "sandbox"
    root.mkdir()

    args = [
        sys.executable,
        str(VERIFIER),
        "--full",
        "--root",
        str(root),
        "--home",
        str(home),
    ]
    first = subprocess.run(args, env=env, capture_output=True, text=True, cwd=REPO_ROOT)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(args, env=env, capture_output=True, text=True, cwd=REPO_ROOT)
    assert second.returncode == 0, second.stderr


def test_keep_class_paths_are_not_touched(tmp_path: Path):
    """A pre-existing keep-class path must survive a default uninstall."""
    home, env = _sandbox_home(tmp_path)
    root = tmp_path / "sandbox"
    root.mkdir()

    # Pre-populate a keep-class path so we can prove it survives.
    ha_dir = root / str(home).lstrip("/") / "homeassistant"
    ha_dir.mkdir(parents=True, exist_ok=True)
    (ha_dir / "configuration.yaml").write_text("# operator owned\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--full",
            "--root",
            str(root),
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert (ha_dir / "configuration.yaml").exists(), (
        "default uninstall must not touch ~/homeassistant/"
    )


def test_report_mode_prints_table_and_exits_zero(tmp_path: Path):
    """--report is informational and never fails."""
    home, env = _sandbox_home(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--report",
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0
    # Header columns should be present.
    for header in ("PATH", "CLASS", "STATUS", "OWNER"):
        assert header in proc.stdout, f"report missing header {header}"
