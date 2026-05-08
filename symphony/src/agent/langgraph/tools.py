from __future__ import annotations

import subprocess
from pathlib import Path

ALLOWED_COMMANDS = {
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("node", "--test"),
    ("npx", "tsc"),
}


def run_allowed(command: list[str], cwd: Path, timeout_seconds: int = 120) -> str:
    if len(command) < 2 or tuple(command[:2]) not in ALLOWED_COMMANDS:
        raise RuntimeError(f"command not allowed: {' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        timeout=timeout_seconds,
        text=True,
        capture_output=True,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"command failed with exit code {result.returncode}")
    return output
