from __future__ import annotations

import re
import subprocess
from pathlib import Path


def safe_worktree_name(identifier: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", identifier.strip()).strip("-").lower()
    return cleaned or "issue"


def branch_name_for_issue(identifier: str) -> str:
    return f"langgraph/{identifier}"


def _branch_exists(repo_root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
        text=True,
    )
    return result.returncode == 0


def next_available_worktree_target(repo_root: Path, identifier: str, base_dir: Path) -> tuple[Path, str]:
    base_name = safe_worktree_name(identifier)
    base_branch = branch_name_for_issue(identifier)
    for suffix in ("", *[f"-{i}" for i in range(2, 100)]):
        path = base_dir / f"{base_name}{suffix}"
        branch = f"{base_branch}{suffix}"
        if not path.exists() and not _branch_exists(repo_root, branch):
            return path, branch
    raise RuntimeError(f"no available worktree target for {identifier}")


def create_issue_worktree(repo_root: Path, identifier: str, base_dir: Path) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    worktree_path, branch = next_available_worktree_target(repo_root, identifier, base_dir)
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )
    return worktree_path


def assert_clean(worktree_path: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree_path,
        check=True,
        text=True,
        capture_output=True,
    )
    if result.stdout.strip():
        raise RuntimeError(f"worktree is not clean: {result.stdout.strip()}")
