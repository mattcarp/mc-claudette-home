from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import shlex
import subprocess
import threading
from typing import Callable

from events import emit
from model_router import DEFAULT_REVIEWER_MODEL
from planner import batch_has_conflicts
from reviewer import ReviewResult, llm_review_diff, review_worktree
from state import IssuePlan, ParallelBatch, WorkerResult
from worktree import create_issue_worktree

DONE_MARKER = "[symphony:done]"
DEFAULT_WORKER_COMMAND = "aider"
DEFAULT_WORKER_MODEL = "deepseek/deepseek-v4-pro"
WORKER_PROMPT_FILE = ".langgraph-worker-prompt.md"
def _worker_map_tokens() -> str:
    """Aider's repo-map size in tokens.

    Default 0 preserves workshop's tuned-for-speed behavior where the worker
    is expected to be handed explicit chat files via worker_chat_files(). For
    cross-repo dispatch (e.g. running BRF tickets through the mc-magpie
    supervisor) the predicted-path -> chat-files map doesn't cover the target
    repo, so operators set LANGGRAPH_WORKER_MAP_TOKENS=1024 (or higher) to
    let Aider discover relevant files on its own.
    """
    raw = os.environ.get("LANGGRAPH_WORKER_MAP_TOKENS")
    if raw is None:
        return "1024"
    raw = raw.strip()
    if not raw:
        return "1024"
    try:
        n = max(0, int(raw))
    except ValueError:
        return "1024"
    return str(n)


def _worker_edit_format() -> str | None:
    """Aider edit format: whole | diff | diff-fenced | udiff.

    Aider's default for unknown models is `whole`, which forces the worker to
    regenerate entire files in its response. For models with strong tool
    obedience (Kimi K2.6, GPT-5.5) we want `diff-fenced` -- much faster, far
    less likely to truncate. Returning None means "do not pass --edit-format
    on the CLI" so Aider picks its own default per-model setting.
    """
    raw = os.environ.get("LANGGRAPH_WORKER_EDIT_FORMAT")
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw in {"whole", "diff", "diff-fenced", "udiff"}:
        return raw
    return None


WORKER_FLAG_BASE = (
    "--no-stream",
    "--no-pretty",
    "--yes-always",
    "--no-check-update",
    "--no-show-release-notes",
    "--no-show-model-warnings",
    "--no-gitignore",
    "--no-analytics",
    "--no-fancy-input",
    # Aider's --yes-always covers file additions but NOT shell command
    # execution. When Kimi suggests `grep -rn ...` to find files, Aider
    # prompts "Run shell commands? (Y/N)" and gets no answer in non-tty
    # mode, then proceeds without the requested context. Disabling shell
    # suggestions entirely keeps Kimi on the diff-emit path.
    "--no-suggest-shell-commands",
    "--model-settings-file",
    "symphony/aider-model-settings.yml",
    "--model-metadata-file",
    "symphony/aider-model-metadata.json",
)


def _worker_flags() -> tuple[str, ...]:
    base: tuple[str, ...] = (*WORKER_FLAG_BASE, "--map-tokens", _worker_map_tokens())
    edit_fmt = _worker_edit_format()
    if edit_fmt is not None:
        base = (*base, "--edit-format", edit_fmt)
    return base


WORKER_FLAGS = _worker_flags()

WorkerStartedHook = Callable[[IssuePlan, Path], None]
WorkerResultHook = Callable[[IssuePlan, Path, WorkerResult], None]
ClassificationBlockedHook = Callable[[IssuePlan, str], None]


def worker_model() -> str:
    return (
        os.environ.get("LANGGRAPH_WORKER_MODEL")
        or os.environ.get("LANGGRAPH_MODEL")
        or DEFAULT_WORKER_MODEL
    )


def reviewer_model() -> str:
    return (
        os.environ.get("LANGGRAPH_REVIEWER_MODEL")
        or DEFAULT_REVIEWER_MODEL
    )


def worker_timeout_seconds() -> int:
    raw = os.environ.get("LANGGRAPH_WORKER_TIMEOUT_SECONDS")
    if not raw:
        return 900
    try:
        return max(1, int(raw))
    except ValueError:
        return 900


def build_worker_command(prompt_file: Path, chat_files: tuple[str, ...] = ()) -> list[str]:
    command = shlex.split(os.environ.get("LANGGRAPH_WORKER_COMMAND", DEFAULT_WORKER_COMMAND))
    if not command:
        command = [DEFAULT_WORKER_COMMAND]
    return [
        *command,
        *chat_files,
        *_worker_flags(),
        "--model",
        worker_model(),
        "--message-file",
        str(prompt_file),
    ]


def worker_chat_files(plan: IssuePlan) -> tuple[str, ...]:
    paths = set(plan.predicted_paths)
    title = f"{plan.issue.title} {plan.issue.description}".lower()
    files: list[str] = []
    if "dashboard/" in paths:
        files.extend(("dashboard/symphony-overview.mjs", "dashboard/symphony-overview.test.mjs"))
    if "screencast/" in paths:
        files.extend(
            (
                "screencast/README.md",
                "screencast/package.json",
                "screencast/pipeline/types.ts",
                "screencast/pipeline/run.ts",
                "screencast/pipeline/dailies.ts",
                "screencast/compose/package.json",
                "screencast/compose/src/Root.tsx",
                "screencast/compose/src/Composition.tsx",
            )
        )
        if any(word in title for word in ("failure", "regression", "test failure")):
            files.extend(
                (
                    "screencast/pipeline/failures.ts",
                    "screencast/pipeline/failure-parser.ts",
                    "screencast/pipeline/tests/failure-parser.test.ts",
                    "screencast/compose/src/compositions/FailureReel.tsx",
                )
            )
        if any(word in title for word in ("whisper", "caption", "subtitle", ".srt", "srt")):
            files.extend(
                (
                    "screencast/pipeline/whisper.ts",
                    "screencast/pipeline/srt.ts",
                    "screencast/pipeline/tests/srt.test.ts",
                )
            )
        if any(word in title for word in ("side-by-side", "multi-device", "viewport")):
            files.extend(
                (
                    "screencast/compose/src/compositions/SideBySide.tsx",
                    "screencast/compose/remotion.config.ts",
                    "screencast/capture/playwright-dual.config.ts",
                    "screencast/capture/record.ts",
                )
            )
        if any(word in title for word in ("webhook", "pr merge", "trigger")):
            files.extend(
                (
                    "screencast/pipeline/webhook.ts",
                    "screencast/pipeline/trigger.ts",
                    "screencast/pipeline/tests/webhook.test.ts",
                    "screencast/screencast-webhook.service",
                )
            )
        if any(word in title for word in ("linear", "ticket overlay", "ticket status")):
            files.extend(
                (
                    "screencast/pipeline/linear.ts",
                    "screencast/compose/src/components/LinearTicketOverlay.tsx",
                )
            )
    if "planner/" in paths:
        files.extend(
            (
                "planner/brainstorm-to-linear.mjs",
                "planner/mc-ticket",
                "planner/mc-ticket-prompt.md",
                "planner/team-keys.json",
            )
        )
    return tuple(dict.fromkeys(files))


def worker_test_commands(plan: IssuePlan) -> tuple[tuple[str, ...], ...]:
    """Return test commands to run inside the worktree after the worker finishes.

    Dispatch is fail-closed: if a predicted-path family is registered here we MUST
    run the corresponding tests before approving. Path families with no entry
    return an empty tuple, which `run_post_worker_tests` reports as 'skipped'.

    Currently registered families:
      - ``dashboard/`` -> ``node --test dashboard/symphony-overview.test.mjs``
        (Node native, no node_modules needed.)
      - ``summary/`` -> ``bash -n summary/symphony-summary.sh``
        (Bash syntax check; full integration tests require live Linear/Telegram.)

    Not yet registered (need worktree-local node_modules to run):
      - ``symphony/`` (vitest)
      - ``screencast/pipeline/`` (node:test --import tsx)
    """
    paths = set(plan.predicted_paths)
    commands: list[tuple[str, ...]] = []
    if "dashboard/" in paths:
        commands.append(("node", "--test", "dashboard/symphony-overview.test.mjs"))
    if "summary/" in paths:
        commands.append(("bash", "-n", "summary/symphony-summary.sh"))
    return tuple(commands)


def capture_head_sha(worktree: Path) -> str:
    """Return the git HEAD SHA at ``worktree``.

    Used to detect workers that exit cleanly without producing any commit, which
    must not pass review even if their stdout contains a [symphony:done] marker.
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def worker_heartbeat_interval_seconds() -> float:
    raw = os.environ.get("LANGGRAPH_WORKER_HEARTBEAT_SECONDS")
    if not raw:
        return 30.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 30.0


def run_worker_with_heartbeat(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    identifier: str,
    interval_seconds: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ``command`` while emitting periodic ``worker_heartbeat`` events.

    The orchestrator's reconcile loop tears a turn down when it sees no stdout
    activity for ~5 minutes. Real DeepSeek calls are silent for longer than
    that, so we emit a heartbeat every ``interval_seconds`` (default 30s) so
    the parent sees liveness without spamming the LLM with extra prompts.
    """
    interval = interval_seconds if interval_seconds is not None else worker_heartbeat_interval_seconds()
    stop = threading.Event()

    def heartbeat() -> None:
        elapsed = 0.0
        while not stop.wait(interval):
            elapsed += interval
            emit("worker_heartbeat", identifier=identifier, elapsed_seconds=round(elapsed, 1))

    thread = threading.Thread(target=heartbeat, daemon=True, name=f"worker-heartbeat-{identifier}")
    thread.start()
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    finally:
        stop.set()
        thread.join(timeout=2)


def post_worker_test_timeout_seconds() -> int:
    raw = os.environ.get("LANGGRAPH_TEST_TIMEOUT_SECONDS")
    if not raw:
        return 180
    try:
        return max(1, int(raw))
    except ValueError:
        return 180


def _command_missing_targets(command: tuple[str, ...], worktree: Path) -> list[str]:
    """Return positional path-like args in ``command`` not present under ``worktree``.

    Used to fail-open on cross-repo dispatch: a registered test command's target
    script (e.g. ``summary/symphony-summary.sh`` from the mc-magpie supervisor's
    test map) may not exist in the dispatched repo (e.g. mc-briefings). Rather
    than report a confusing ``worker_tests_failed`` (file not found), the caller
    skips such commands and proceeds to LLM review with a clear ``skipped``
    event. A path-like arg is one containing ``/`` and not starting with ``-``.
    """
    missing: list[str] = []
    for arg in command:
        if not arg or arg.startswith("-"):
            continue
        if "/" not in arg:
            continue
        if not (worktree / arg).exists():
            missing.append(arg)
    return missing


def run_post_worker_tests(plan: IssuePlan, worktree: Path) -> ReviewResult:
    """Run registered post-worker tests inside ``worktree``.

    Returns ``ok=True`` with a 'skipped' event when no commands are registered for
    the plan's predicted paths, ``ok=True`` when every command exits 0, and
    ``ok=False`` (with stderr/stdout tail in the reason) on the first non-zero
    exit or timeout. Commands whose target script is missing from the worktree
    (cross-repo dispatch) are skipped per-command rather than failing the gate.
    """
    commands = worker_test_commands(plan)
    if not commands:
        emit(
            "worker_tests_skipped",
            identifier=plan.issue.identifier,
            reason="no test commands registered for predicted paths",
            predicted_paths=list(plan.predicted_paths),
        )
        return ReviewResult(True, "no test commands registered for predicted paths", ())

    rendered: list[str] = []
    skipped: list[str] = []
    ran = 0
    timeout = post_worker_test_timeout_seconds()
    for command in commands:
        missing = _command_missing_targets(command, worktree)
        if missing:
            rendered.append(f"# skipped (missing {','.join(missing)}): {' '.join(command)}")
            skipped.append(" ".join(command))
            emit(
                "worker_tests_skipped",
                identifier=plan.issue.identifier,
                command=" ".join(command),
                reason=f"command target(s) not in worktree: {missing}",
            )
            continue
        rendered.append(" ".join(command))
        try:
            completed = subprocess.run(
                list(command),
                cwd=worktree,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            reason = f"test command not found: {command[0]} ({exc})"
            emit(
                "worker_tests_failed",
                identifier=plan.issue.identifier,
                command=" ".join(command),
                reason=reason,
            )
            return ReviewResult(False, reason, tuple(rendered))
        except subprocess.TimeoutExpired as exc:
            reason = (
                f"test command timed out after {timeout}s: {' '.join(command)}: "
                f"{_output_tail(exc.stdout)} {_output_tail(exc.stderr)}"
            ).strip()
            emit(
                "worker_tests_failed",
                identifier=plan.issue.identifier,
                command=" ".join(command),
                reason=reason,
            )
            return ReviewResult(False, reason, tuple(rendered))

        if completed.returncode != 0:
            reason = (
                f"{' '.join(command)} exited with code {completed.returncode}: "
                f"{_output_tail(completed.stdout)} {_output_tail(completed.stderr)}"
            ).strip()
            emit(
                "worker_tests_failed",
                identifier=plan.issue.identifier,
                command=" ".join(command),
                exit_code=completed.returncode,
                reason=reason,
            )
            return ReviewResult(False, reason, tuple(rendered))
        ran += 1

    if ran == 0:
        # All registered commands were skipped (cross-repo: scripts not in worktree).
        return ReviewResult(
            True,
            f"all test commands skipped (targets not in worktree): {skipped}",
            tuple(rendered),
        )

    emit(
        "worker_tests_passed",
        identifier=plan.issue.identifier,
        commands=rendered,
    )
    return ReviewResult(True, "post-worker tests passed", tuple(rendered))


def write_worker_prompt(plan: IssuePlan, worktree_path: Path) -> Path:
    issue = plan.issue
    prompt_file = worktree_path / WORKER_PROMPT_FILE
    predicted_paths = "\n".join(f"- {path}" for path in plan.predicted_paths) or "- (none predicted)"
    description = issue.description.strip() or "(no description)"
    stack_guidance = _stack_guidance(plan)
    prompt_file.write_text(
        (
            f"# Linear issue {issue.identifier}: {issue.title}\n\n"
            "Implement the minimum shippable fix for this Linear issue in this worktree.\n"
            "You have full permission to create new files or modify any existing file in the repo.\n"
            "Use the repo-map to discover relevant files and implementation patterns.\n"
            "Do not wait for human help or ask for more files — you must complete the task using what you can find.\n"
            "Do not push. Keep changes focused and run relevant verification before finishing.\n"
            "Follow the existing repo stack and test style. Do not invent a new language, framework, or package path.\n"
            f"If the work is complete, include {DONE_MARKER} in the final worker output.\n\n"
            "## Hygiene rules (enforced by deterministic review)\n\n"
            "- No trailing whitespace on any line (including markdown). The\n"
            "  deterministic gate runs `git diff --check` and rejects the patch\n"
            "  if it finds any. This is the most common cause of review_failed.\n"
            "- No filenames containing spaces, shell metacharacters, or paths\n"
            "  that look like commands (e.g. `node --test foo.js`).\n"
            "- One concern per commit; multi-line markdown tables are fine but\n"
            "  trim every cell.\n\n"
            "## File-listing protocol (IMPORTANT)\n\n"
            "If you need additional files added to the chat before you can proceed,\n"
            "list each one on its own line wrapped in single backticks, with no\n"
            "numbering, bold formatting, descriptions, or other prose on that line.\n"
            "The auto-add parser only recognizes the bare-backtick form on its own\n"
            "line. Decorated variants (numbered list `1. ...`, bullets, bold\n"
            "`**path**`, trailing descriptions `path -- explanation`) are silently\n"
            "dropped, leaving you with no file context.\n\n"
            "Use the actual paths from the repo-map (e.g. real source/test files\n"
            "you've identified). Format example using placeholder names so this\n"
            "instruction itself does not get auto-added:\n\n"
            "    `<src/path/to/source-file>`\n"
            "    `<src/path/to/test-file>`\n\n"
            "When creating brand-new files (e.g. docs, new tests, new sources)\n"
            "you do NOT need to request them first. Emit a udiff with\n"
            "`--- /dev/null` and `+++ path/to/new-file.ext` directly. Aider's\n"
            "udiff applier creates the file from the diff.\n\n"
            "Prefer producing edits directly in your first response when the\n"
            "repo-map gives you enough signal. Only request files when the\n"
            "repo-map is genuinely insufficient AND the work modifies existing\n"
            "code.\n\n"
            "## Description\n\n"
            f"{description}\n\n"
            f"{stack_guidance}"
            "## Planner context\n\n"
            f"- Classification: {plan.classification}\n"
            f"- Risk: {plan.risk}\n"
            f"- Reason: {plan.reason}\n"
            "- Predicted paths:\n"
            f"{predicted_paths}\n"
        ),
        encoding="utf-8",
    )
    return prompt_file


def _stack_guidance(plan: IssuePlan) -> str:
    paths = set(plan.predicted_paths)
    guidance: list[str] = []
    if "dashboard/" in paths:
        guidance.append(
            "## Dashboard stack guidance\n\n"
            "- Existing dashboard code is Node ESM in `dashboard/symphony-overview.mjs`.\n"
            "- Existing dashboard tests use `node:test` in `dashboard/symphony-overview.test.mjs`.\n"
            "- Add or update `.mjs` tests near the dashboard code and run them with Node's test runner.\n"
            "- Do not create Go tests, Go modules, placeholder import paths, or unrelated `tests/*.go` files.\n"
        )
    if "screencast/" in paths:
        guidance.append(
            "## Screencast stack guidance\n\n"
            "- Existing screencast pipeline code is TypeScript under `screencast/pipeline/`.\n"
            "- Existing Remotion code is under `screencast/compose/src/`; the root composition file is `screencast/compose/src/Root.tsx`.\n"
            "- Existing shot capture code is under `screencast/capture/`.\n"
            "- Existing pipeline tests live in `screencast/pipeline/tests/` and run through the screencast package scripts.\n"
            "- Do not create Python screencast modules, `screencast/src/*`, Go tests, or placeholder package paths.\n"
        )
    if not guidance:
        return ""
    return "\n".join(guidance) + "\n"


def execute_one_plan(
    plan: IssuePlan,
    repo_root: Path,
    worker_root: Path,
    on_worker_started: WorkerStartedHook | None = None,
    on_worker_failed: WorkerResultHook | None = None,
    on_review_passed: WorkerResultHook | None = None,
    on_classification_blocked: ClassificationBlockedHook | None = None,
    allow_serial: bool = False,
) -> WorkerResult:
    issue = plan.issue
    can_execute_parallel = plan.classification == "ready_parallel" and plan.risk == "low"
    can_execute_serial = allow_serial and plan.classification == "ready_serial" and plan.risk == "medium"
    if not (can_execute_parallel or can_execute_serial):
        reason = "Only low-risk ready_parallel issues can execute in this mode"
        emit("worker_blocked", identifier=issue.identifier, reason=reason)
        if on_classification_blocked:
            on_classification_blocked(plan, reason)
        return WorkerResult(issue.identifier, False, reason)

    try:
        path = create_issue_worktree(repo_root, issue.identifier, worker_root)
    except Exception as exc:
        reason = str(exc)
        emit("worker_blocked", identifier=issue.identifier, reason=reason)
        return WorkerResult(issue.identifier, False, reason)

    emit("worker_started", identifier=issue.identifier, worktree=str(path))
    if on_worker_started:
        on_worker_started(plan, path)

    # Pre-seed the worktree-local git exclude so aider runtime files are never staged.
    # In a worktree the .git entry is a file; resolve the actual git dir via rev-parse.
    try:
        git_dir_result = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=path, text=True, capture_output=True, check=True
        )
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = path / git_dir
        exclude_file = git_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        if ".aider" not in existing:
            with open(exclude_file, "a", encoding="utf-8") as f:
                f.write("\n# Aider runtime files (LangGraph worker — do not stage)\n.aider*\n")
    except Exception:
        pass  # non-fatal; the reviewer pathspec exclusion is the backup

    prompt_file = write_worker_prompt(plan, path)
    command = build_worker_command(prompt_file, worker_chat_files(plan))
    try:
        start_sha = capture_head_sha(path)
    except Exception as exc:
        reason = f"could not capture worktree start SHA: {exc}"
        result = WorkerResult(issue.identifier, False, reason, str(path))
        emit("worker_failed", identifier=issue.identifier, reason=reason)
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    try:
        completed = run_worker_with_heartbeat(
            command,
            cwd=path,
            timeout=worker_timeout_seconds(),
            identifier=issue.identifier,
        )
    except FileNotFoundError:
        reason = f"worker command not found: {command[0]}"
        result = WorkerResult(issue.identifier, False, reason, str(path))
        emit("worker_failed", identifier=issue.identifier, reason=reason)
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result
    except subprocess.TimeoutExpired as exc:
        reason = f"worker timed out after {worker_timeout_seconds()}s: {_output_tail(exc.stdout)} {_output_tail(exc.stderr)}".strip()
        result = WorkerResult(issue.identifier, False, reason, str(path))
        emit("worker_failed", identifier=issue.identifier, reason=reason)
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    combined_output = f"{completed.stdout}\n{completed.stderr}"
    done_marker = DONE_MARKER in combined_output.lower()

    # Capture HEAD before deciding on exit code so we can distinguish a
    # genuine worker failure (no commit produced) from a post-commit crash
    # (Aider applied edits, committed, then died inside its own --lint /
    # cleanup / __init__ path). The commit itself is the source of truth;
    # if it landed we proceed to review even if Aider's own exit was angry.
    try:
        end_sha = capture_head_sha(path)
    except Exception as exc:
        reason = f"could not capture worktree HEAD after worker: {exc}"
        result = WorkerResult(issue.identifier, False, reason, str(path), done_marker)
        emit("worker_failed", identifier=issue.identifier, reason=reason)
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    worker_committed = end_sha != start_sha

    if completed.returncode != 0 and not worker_committed:
        # Genuine failure: non-zero exit AND no commit.
        reason = (
            f"worker exited with code {completed.returncode}: "
            f"{_output_tail(completed.stdout)} {_output_tail(completed.stderr)}"
        ).strip()
        result = WorkerResult(issue.identifier, False, reason, str(path), done_marker)
        emit(
            "worker_failed",
            identifier=issue.identifier,
            reason=reason,
            exit_code=completed.returncode,
            stdout_bytes=len(completed.stdout.encode("utf-8")),
            stderr_bytes=len(completed.stderr.encode("utf-8")),
        )
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    if completed.returncode != 0 and worker_committed:
        # Aider crashed post-commit. The commit is real; surface a warning
        # event for visibility but proceed to the review pipeline. Without
        # this branch we silently throw away every Aider __init__() bug
        # commit and burn another OpenRouter turn for nothing.
        emit(
            "worker_post_commit_crash",
            identifier=issue.identifier,
            exit_code=completed.returncode,
            start_sha=start_sha,
            end_sha=end_sha,
            reason=_output_tail(completed.stderr),
        )

    emit(
        "worker_completed",
        identifier=issue.identifier,
        exit_code=completed.returncode,
        stdout_bytes=len(completed.stdout.encode("utf-8")),
        stderr_bytes=len(completed.stderr.encode("utf-8")),
    )

    if end_sha == start_sha:
        reason = (
            "worker produced no new commit (HEAD unchanged after run); "
            "refusing to mark Done with empty diff"
        )
        result = WorkerResult(issue.identifier, False, reason, str(path), done_marker)
        emit(
            "worker_no_changes",
            identifier=issue.identifier,
            start_sha=start_sha,
            end_sha=end_sha,
            stdout_bytes=len(completed.stdout.encode("utf-8")),
        )
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    det_review = review_worktree(path)
    if not det_review.ok:
        result = WorkerResult(issue.identifier, False, det_review.reason, str(path), done_marker)
        emit("review_failed", identifier=issue.identifier, stage="deterministic", reason=det_review.reason, commands=list(det_review.commands))
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    emit("deterministic_review_passed", identifier=issue.identifier, commands=list(det_review.commands))

    test_review = run_post_worker_tests(plan, path)
    if not test_review.ok:
        result = WorkerResult(issue.identifier, False, test_review.reason, str(path), done_marker)
        emit(
            "review_failed",
            identifier=issue.identifier,
            stage="tests",
            reason=test_review.reason,
            commands=list(test_review.commands),
        )
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    llm_review = llm_review_diff(
        path,
        issue_title=issue.title,
        issue_description=issue.description,
        worker_output=combined_output,
        reviewer_model=reviewer_model(),
    )
    if not llm_review.ok:
        result = WorkerResult(issue.identifier, False, llm_review.reason, str(path), done_marker)
        emit("review_failed", identifier=issue.identifier, stage="llm", reason=llm_review.reason, reviewer=reviewer_model())
        if on_worker_failed:
            on_worker_failed(plan, path, result)
        return result

    result = WorkerResult(issue.identifier, True, llm_review.reason, str(path), done_marker)
    emit("review_passed", identifier=issue.identifier, reason=llm_review.reason, reviewer=reviewer_model())
    if on_review_passed:
        on_review_passed(plan, path, result)
    return result


def _output_tail(output: str | bytes | None, limit: int = 1200) -> str:
    if not output:
        return ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output.strip()[-limit:]


def execute_batch(
    batch: ParallelBatch,
    repo_root: Path,
    worker_root: Path,
    max_workers: int,
    on_worker_started: WorkerStartedHook | None = None,
    on_worker_failed: WorkerResultHook | None = None,
    on_review_passed: WorkerResultHook | None = None,
) -> list[WorkerResult]:
    if batch_has_conflicts(batch):
        reason = "Batch contains overlapping predicted paths"
        for plan in batch.plans:
            emit("worker_blocked", identifier=plan.issue.identifier, reason=reason)
        return [WorkerResult(plan.issue.identifier, False, reason) for plan in batch.plans]

    identifiers = [plan.issue.identifier for plan in batch.plans]
    emit("parallel_batch_started", batch_id=batch.batch_id, identifiers=identifiers)
    results: list[WorkerResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                execute_one_plan,
                plan,
                repo_root,
                worker_root,
                on_worker_started,
                on_worker_failed,
                on_review_passed,
            )
            for plan in batch.plans
        ]
        for future in as_completed(futures):
            results.append(future.result())

    emit(
        "parallel_batch_completed",
        batch_id=batch.batch_id,
        passed=sorted(result.identifier for result in results if result.ok),
        failed=sorted(result.identifier for result in results if not result.ok),
    )
    return results
