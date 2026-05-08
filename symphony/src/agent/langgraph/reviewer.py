from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]


DEFAULT_REVIEWER_MODEL = "openai/gpt-5.5"

LLM_REVIEW_PROMPT = """\
You are a senior engineer doing a focused code review on a diff produced by an automated agent.

Issue: {title}
Description: {description}

Git diff:
```
{diff}
```

Worker output (last 2000 chars):
```
{worker_output}
```

Review task: Decide whether the diff is safe to merge for human review.
Return exactly one of these two responses:
  PASS: <one short reason>
  FAIL: <one short reason>

Rules:
- PASS if the changes are relevant to the issue and no obvious regressions or unsafe code is introduced.
- FAIL if the diff is empty (no real changes).
- FAIL if the code references new files, imports, or scripts that are NOT present in the diff (incomplete wiring).
- FAIL if the worker output suggests they were stuck or waiting for files.
- Do not fail for style. Minor imperfections are fine.
- Respond ONLY with PASS or FAIL line; nothing else.
"""

MAX_DIFF_BYTES = 12000
MAX_WORKER_OUTPUT_BYTES = 2000
IGNORED_UNTRACKED = {".langgraph-worker-prompt.md"}


@dataclass(frozen=True)
class ReviewResult:
    ok: bool
    reason: str
    commands: tuple[str, ...]


def _run(command: tuple[str, ...], cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    return (result.stdout + result.stderr).strip()


def _has_parent_commit(cwd: Path) -> bool:
    result = subprocess.run(("git", "rev-parse", "--verify", "HEAD~1"), cwd=cwd, text=True, capture_output=True)
    return result.returncode == 0


# Worker workflows occasionally invent a file whose path is actually the
# command they meant to suggest running (e.g. "node --test foo.mjs" became
# a real file in MAG-48). Reject paths that contain whitespace or start with
# a shell-command-shaped leading token; legitimate code paths never look like
# that in this repo.
_COMMAND_LEADERS = ("node ", "npm ", "npx ", "bash ", "yarn ", "pnpm ", "python ", "tsx ")


def _suspicious_added_paths(cwd: Path) -> list[str]:
    if not _has_parent_commit(cwd):
        return []
    result = subprocess.run(
        ("git", "diff", "HEAD~1..HEAD", "--name-only", "--diff-filter=A"),
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    flagged: list[str] = []
    for raw in result.stdout.splitlines():
        path = raw.strip()
        if not path or path.startswith(".aider"):
            continue
        if any(ch in path for ch in (" ", "\t")):
            flagged.append(path)
            continue
        for leader in _COMMAND_LEADERS:
            if path.startswith(leader.strip()) and "-" not in path.split("/")[0]:
                # path starts with a command-shaped word and has no dash in
                # the first segment (so 'node-foo' or 'npm-pkg' is fine but
                # bare 'node' alone is not). Belt-and-braces: the whitespace
                # check above is the primary signal; this is a fallback.
                flagged.append(path)
                break
    return flagged


def _untracked_review_files(cwd: Path) -> list[str]:
    result = subprocess.run(
        ("git", "status", "--short", "--untracked-files=all"),
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        if path in IGNORED_UNTRACKED or path.startswith(".aider"):
            continue
        files.append(path)
    return files


def review_worktree(worktree_path: Path) -> ReviewResult:
    # git diff --check flags trailing whitespace in staged changes including aider
    # runtime files (.aider.chat.history.md). Exclude those from the check since
    # they are generated metadata, not code we own.
    commands: tuple[tuple[str, ...], ...] = (
        ("git", "status", "--short"),
        ("git", "diff", "--check", "--", ":!.aider*"),
        ("git", "diff", "--cached", "--check", "--", ":!.aider*"),
    )
    if _has_parent_commit(worktree_path):
        commands += (("git", "diff", "--check", "HEAD~1..HEAD", "--", ":!.aider*"),)

    labels = tuple(" ".join(cmd) for cmd in commands)
    for command in commands:
        result = subprocess.run(command, cwd=worktree_path, text=True, capture_output=True)
        if result.returncode != 0:
            return ReviewResult(False, (result.stdout + result.stderr).strip(), labels)
    untracked = _untracked_review_files(worktree_path)
    if untracked:
        return ReviewResult(False, f"untracked review file(s) not in git diff: {', '.join(untracked)}", labels)
    suspicious = _suspicious_added_paths(worktree_path)
    if suspicious:
        return ReviewResult(
            False,
            f"suspicious added path(s) (whitespace / shell-command-shaped name): {', '.join(suspicious)}",
            labels,
        )
    return ReviewResult(True, "deterministic checks passed", labels)


def _is_openai_provider(model_str: str) -> bool:
    """Return True if the model string targets the OpenAI client.

    Bare names (e.g. 'gpt-4o') are assumed OpenAI.
    'openai/...' prefixed names are OpenAI.
    Any other provider prefix (e.g. 'deepseek/...', 'anthropic/...') returns False.
    """
    if "/" not in model_str:
        return True
    provider, _ = model_str.split("/", 1)
    return provider.lower() == "openai"


def _openai_model_name(model_str: str) -> str:
    """Strip openai/ prefix if present; leave other strings unchanged."""
    if model_str.startswith("openai/"):
        return model_str[len("openai/"):]
    return model_str


_REASONING_PREFIXES = ("o1", "o3", "gpt-5")


def _is_reasoning_model(model_name: str) -> bool:
    """Reasoning models (o1, o3, gpt-5.x) reject the temperature parameter."""
    bare_name = model_name.split("/", 1)[1] if "/" in model_name else model_name
    return any(bare_name.startswith(p) for p in _REASONING_PREFIXES)


def _resolve_reviewer_api_key() -> str | None:
    """Pick the API key for the LLM reviewer.

    Order of precedence:
      1. ``LANGGRAPH_REVIEWER_API_KEY`` (explicit override).
      2. ``OPENROUTER_API_KEY`` if a base URL is configured (so OpenRouter is
         used when the operator points us at it).
      3. ``OPENAI_API_KEY``.
    """
    explicit = os.environ.get("LANGGRAPH_REVIEWER_API_KEY")
    if explicit:
        return explicit
    if os.environ.get("LANGGRAPH_REVIEWER_BASE_URL"):
        router_key = os.environ.get("OPENROUTER_API_KEY")
        if router_key:
            return router_key
    return os.environ.get("OPENAI_API_KEY")


def _resolve_reviewer_base_url() -> str | None:
    """Return the base URL to point the OpenAI client at, or None for default."""
    return os.environ.get("LANGGRAPH_REVIEWER_BASE_URL") or None


def llm_review_diff(
    worktree_path: Path,
    issue_title: str,
    issue_description: str,
    worker_output: str,
    reviewer_model: str | None = None,
) -> ReviewResult:
    """Call OpenAI to review the git diff and worker output for a completed worktree."""
    model_str = reviewer_model or os.environ.get("LANGGRAPH_REVIEWER_MODEL") or DEFAULT_REVIEWER_MODEL

    # Fail fast if a non-OpenAI model is routed to this OpenAI-only reviewer.
    if not _is_openai_provider(model_str):
        return ReviewResult(
            False,
            f"reviewer model '{model_str}' is not an OpenAI model; "
            "only openai/* models are supported for LLM review",
            ("llm_review_skipped",),
        )

    api_key = _resolve_reviewer_api_key()
    if not api_key:
        reason = (
            "no reviewer API key set (looked for LANGGRAPH_REVIEWER_API_KEY, "
            "OPENROUTER_API_KEY when LANGGRAPH_REVIEWER_BASE_URL is set, then "
            "OPENAI_API_KEY); LLM review cannot run"
        )
        return ReviewResult(False, reason, ("llm_review_skipped",))
    base_url = _resolve_reviewer_base_url()

    diff = _run(("git", "diff", "HEAD", "--", ":!.aider*", ":!.langgraph-worker-prompt.md"), worktree_path)
    if not diff:
        diff = _run(("git", "diff", "--cached", "--", ":!.aider*", ":!.langgraph-worker-prompt.md"), worktree_path)
    if not diff:
        if _has_parent_commit(worktree_path):
            diff = _run(("git", "diff", "HEAD~1..HEAD", "--", ":!.aider*", ":!.langgraph-worker-prompt.md"), worktree_path)
        else:
            diff = ""

    diff = diff[:MAX_DIFF_BYTES]
    worker_tail = worker_output[-MAX_WORKER_OUTPUT_BYTES:] if worker_output else ""

    prompt = LLM_REVIEW_PROMPT.format(
        title=issue_title,
        description=(issue_description or "(none)").strip(),
        diff=diff or "(empty — no changes detected)",
        worker_output=worker_tail,
    )

    try:
        if OpenAI is None:
            raise ImportError("openai package not installed")
        client_kwargs: dict = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = OpenAI(**client_kwargs)
        # OpenRouter accepts the openai/<name> namespaced form; only strip the
        # prefix when we are talking to OpenAI direct.
        if base_url and "openrouter" in base_url.lower():
            model_name = model_str
        else:
            model_name = _openai_model_name(model_str)
        # Reasoning models (o1/o3/gpt-5.x) require max_completion_tokens;
        # older models use max_tokens. Temperature is unsupported on reasoning models.
        # NOTE: reasoning tokens count against max_completion_tokens. GPT-5.5
        # routinely uses 500-2000 tokens of internal reasoning for code review,
        # so the visible response would be empty if the budget were tight.
        # 4096 leaves comfortable headroom for both reasoning + 1-2 sentence
        # PASS/FAIL response. Operators can tune via LANGGRAPH_REVIEWER_MAX_TOKENS.
        max_tokens_default = 4096 if _is_reasoning_model(model_name) else 512
        try:
            max_tokens = int(os.environ.get("LANGGRAPH_REVIEWER_MAX_TOKENS", "").strip() or max_tokens_default)
        except ValueError:
            max_tokens = max_tokens_default
        if _is_reasoning_model(model_name):
            create_kwargs: dict = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_completion_tokens": max_tokens,
            }
        else:
            create_kwargs = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
            }
        response = client.chat.completions.create(**create_kwargs)
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        reason = f"LLM reviewer error ({type(exc).__name__}): {exc}"
        return ReviewResult(False, reason, ("llm_review",))

    upper = text.upper()
    if upper.startswith("PASS"):
        reason = text[4:].lstrip(": ").strip() or "LLM review passed"
        return ReviewResult(True, f"llm_review passed: {reason}", ("llm_review",))
    if upper.startswith("FAIL"):
        reason = text[4:].lstrip(": ").strip() or "LLM review failed"
        return ReviewResult(False, f"llm_review failed: {reason}", ("llm_review",))

    # Ambiguous response — default safe to fail
    return ReviewResult(False, f"llm_review ambiguous response: {text[:120]}", ("llm_review",))
