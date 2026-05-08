import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reviewer import ReviewResult
from state import IssuePlan, LinearIssue, ParallelBatch, WorkerResult
from supervisor import (
    build_worker_command,
    capture_head_sha,
    execute_batch,
    execute_one_plan,
    run_post_worker_tests,
    run_worker_with_heartbeat,
    worker_chat_files,
    worker_test_commands,
    write_worker_prompt,
)


def _make_issue(identifier: str = "MAG-48", state: str = "Todo") -> LinearIssue:
    return LinearIssue(
        id=identifier.lower().replace("-", ""),
        identifier=identifier,
        title="docs: add runbook",
        description="Add a runbook for the new service.",
        state=state,
        labels=("docs",),
    )


def _make_plan(issue: LinearIssue) -> IssuePlan:
    return IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")


_DET_PASS = ReviewResult(True, "deterministic checks passed", ("git status --short",))
_LLM_PASS = ReviewResult(True, "llm_review passed: changes look relevant", ("llm_review",))
_LLM_FAIL = ReviewResult(False, "llm_review failed: diff is empty", ("llm_review",))


class SupervisorTests(unittest.TestCase):
    def test_worker_command_uses_deepseek_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            command = build_worker_command(Path("/tmp/prompt.md"))

        self.assertEqual(command[0], "aider")
        model_idx = command.index("--model") + 1
        self.assertIn("deepseek", command[model_idx])

    def test_worker_command_respects_worker_model_env(self):
        with patch.dict(
            "os.environ",
            {"LANGGRAPH_WORKER_MODEL": "deepseek/deepseek-v4-pro"},
            clear=True,
        ):
            command = build_worker_command(Path("/tmp/prompt.md"))

        self.assertEqual(command[0], "aider")
        self.assertIn("--message-file", command)
        self.assertIn("/tmp/prompt.md", command)
        self.assertIn("--model", command)
        self.assertIn("deepseek/deepseek-v4-pro", command)
        self.assertIn("--yes-always", command)
        self.assertIn("--no-gitignore", command)
        self.assertIn("--no-show-model-warnings", command)
        self.assertIn("--model-settings-file", command)
        self.assertIn("symphony/aider-model-settings.yml", command)
        self.assertIn("--model-metadata-file", command)
        self.assertIn("symphony/aider-model-metadata.json", command)
        self.assertIn("--map-tokens", command)
        self.assertIn("0", command)

    def test_reviewer_model_is_distinct_from_worker_model(self):
        from model_router import model_route
        with patch.dict("os.environ", {}, clear=True):
            route = model_route()
        self.assertNotEqual(route.worker, route.reviewer)
        self.assertIn("deepseek", route.worker)
        self.assertIn("openai", route.reviewer)

    def test_worker_prompt_includes_dashboard_stack_guidance(self):
        plan = IssuePlan(_make_issue("MAG-68"), "ready_parallel", "low risk", ("dashboard/",), "low")
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = write_worker_prompt(plan, Path(tmp))
            prompt = prompt_path.read_text(encoding="utf-8")

        self.assertIn("dashboard/symphony-overview.mjs", prompt)
        self.assertIn("node:test", prompt)
        self.assertIn("Do not create Go tests", prompt)

    def test_worker_prompt_includes_screencast_stack_guidance(self):
        issue = LinearIssue(
            id="mag39",
            identifier="MAG-39",
            title="Multi-Device Side-by-Side",
            description="Add a Remotion composition for mobile and desktop recordings.",
            state="Todo",
            labels=("brainstorm:brainstorm-screencast",),
        )
        plan = IssuePlan(issue, "ready_serial", "Implementation work should start serially", ("screencast/",), "medium")
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = write_worker_prompt(plan, Path(tmp))
            prompt = prompt_path.read_text(encoding="utf-8")

        self.assertIn("screencast/pipeline/", prompt)
        self.assertIn("screencast/compose/src/Root.tsx", prompt)
        self.assertIn("Do not create Python screencast modules", prompt)

    def test_dashboard_worker_command_preloads_dashboard_files(self):
        plan = IssuePlan(_make_issue("MAG-68"), "ready_parallel", "low risk", ("dashboard/",), "low")
        files = worker_chat_files(plan)
        command = build_worker_command(Path("/tmp/prompt.md"), files)

        self.assertIn("dashboard/symphony-overview.mjs", command)
        self.assertIn("dashboard/symphony-overview.test.mjs", command)

    def test_screencast_worker_command_preloads_real_remotion_files(self):
        issue = LinearIssue(
            id="mag39",
            identifier="MAG-39",
            title="Multi-Device Side-by-Side",
            description="Add a Remotion composition that shows mobile and desktop viewport recordings side-by-side.",
            state="Todo",
            labels=("brainstorm:brainstorm-screencast",),
        )
        plan = IssuePlan(issue, "ready_serial", "Implementation work should start serially", ("screencast/",), "medium")
        files = worker_chat_files(plan)
        command = build_worker_command(Path("/tmp/prompt.md"), files)

        self.assertIn("screencast/README.md", command)
        self.assertIn("screencast/compose/src/Root.tsx", command)
        self.assertIn("screencast/compose/src/compositions/SideBySide.tsx", command)
        self.assertIn("screencast/capture/playwright-dual.config.ts", command)
        self.assertNotIn("screencast/src/Video.tsx", command)

    def test_screencast_worker_command_preloads_webhook_and_planner_files(self):
        issue = LinearIssue(
            id="mag42",
            identifier="MAG-42",
            title="Linear Ticket Overlay",
            description="Automatically query the Linear API to overlay ticket status.",
            state="Todo",
            labels=("brainstorm:brainstorm-screencast",),
        )
        plan = IssuePlan(issue, "ready_serial", "Implementation work should start serially", ("screencast/", "planner/"), "medium")
        files = worker_chat_files(plan)
        command = build_worker_command(Path("/tmp/prompt.md"), files)

        self.assertIn("screencast/pipeline/linear.ts", command)
        self.assertIn("screencast/compose/src/components/LinearTicketOverlay.tsx", command)
        self.assertIn("planner/brainstorm-to-linear.mjs", command)

    def test_execute_one_runs_worker_then_both_reviews_on_success(self):
        issue = _make_issue("MAG-48")
        plan = _make_plan(issue)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "ready [symphony:done]", ""),
                ) as run,
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "end-sha"]),
                patch("supervisor.review_worktree", return_value=_DET_PASS),
                patch("supervisor.llm_review_diff", return_value=_LLM_PASS),
            ):
                with redirect_stdout(buf):
                    result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertTrue(result.ok)
        self.assertTrue(result.done_marker)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "aider")
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_started", events)
        self.assertIn("worker_completed", events)
        self.assertIn("deterministic_review_passed", events)
        self.assertIn("review_passed", events)
        self.assertNotIn("worker_failed", events)
        self.assertNotIn("review_failed", events)

    def test_execute_one_llm_review_failure_blocks_success(self):
        issue = _make_issue("MAG-49")
        plan = _make_plan(issue)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "done", ""),
                ),
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "end-sha"]),
                patch("supervisor.review_worktree", return_value=_DET_PASS),
                patch("supervisor.llm_review_diff", return_value=_LLM_FAIL),
            ):
                with redirect_stdout(buf):
                    result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertFalse(result.ok)
        self.assertIn("llm_review failed", result.reason)
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_completed", events)
        self.assertIn("deterministic_review_passed", events)
        self.assertIn("review_failed", events)
        self.assertNotIn("review_passed", events)

        review_failed_events = [
            json.loads(line)
            for line in buf.getvalue().splitlines()
            if json.loads(line)["event"] == "review_failed"
        ]
        self.assertEqual(review_failed_events[0]["stage"], "llm")

    def test_execute_one_deterministic_failure_skips_llm(self):
        issue = _make_issue("MAG-50")
        plan = _make_plan(issue)
        det_fail = ReviewResult(False, "whitespace error in foo.py", ("git diff --check",))
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "done", ""),
                ),
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "end-sha"]),
                patch("supervisor.review_worktree", return_value=det_fail),
                patch("supervisor.llm_review_diff") as llm_mock,
            ):
                with redirect_stdout(buf):
                    result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertFalse(result.ok)
        llm_mock.assert_not_called()
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("review_failed", events)

        review_failed_events = [
            json.loads(line)
            for line in buf.getvalue().splitlines()
            if json.loads(line)["event"] == "review_failed"
        ]
        self.assertEqual(review_failed_events[0]["stage"], "deterministic")

    def test_execute_one_emits_worker_failed_on_nonzero_without_review(self):
        issue = _make_issue("MAG-51")
        plan = _make_plan(issue)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 2, "", "boom"),
                ),
                patch("supervisor.review_worktree") as det_mock,
                patch("supervisor.llm_review_diff") as llm_mock,
            ):
                with redirect_stdout(buf):
                    result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertFalse(result.ok)
        self.assertIn("worker exited with code 2", result.reason)
        self.assertIn("boom", result.reason)
        det_mock.assert_not_called()
        llm_mock.assert_not_called()
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_failed", events)
        self.assertNotIn("review_passed", events)

    def test_execute_one_uses_worker_timeout(self):
        issue = _make_issue("MAG-73")
        plan = _make_plan(issue)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict("os.environ", {"LANGGRAPH_WORKER_TIMEOUT_SECONDS": "12"}),
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "ready [symphony:done]", ""),
                ) as run,
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "end-sha"]),
                patch("supervisor.review_worktree", return_value=_DET_PASS),
                patch("supervisor.llm_review_diff", return_value=_LLM_PASS),
            ):
                execute_one_plan(plan, Path(tmp), Path(tmp))

        # Find the aider invocation; capture_head_sha runs separately so timeouts vary.
        aider_call = next(c for c in run.call_args_list if c.args[0][0] == "aider")
        self.assertEqual(aider_call.kwargs["timeout"], 12)

    def test_execute_one_allows_ready_serial_when_serial_gate_enabled(self):
        issue = _make_issue("MAG-70")
        plan = IssuePlan(issue, "ready_serial", "Implementation work should start serially", ("dashboard/",), "medium")
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "ready [symphony:done]", ""),
                ),
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "end-sha"]),
                patch("supervisor.review_worktree", return_value=_DET_PASS),
                patch("supervisor.llm_review_diff", return_value=_LLM_PASS),
            ):
                result = execute_one_plan(plan, Path(tmp), Path(tmp), allow_serial=True)

        self.assertTrue(result.ok)

    def test_execute_one_fails_when_worker_makes_no_new_commit(self):
        issue = _make_issue("MAG-80")
        plan = _make_plan(issue)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "[symphony:done]", ""),
                ),
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "start-sha"]),
                patch("supervisor.review_worktree") as det_mock,
                patch("supervisor.llm_review_diff") as llm_mock,
            ):
                with redirect_stdout(buf):
                    result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertFalse(result.ok)
        self.assertIn("no new commit", result.reason.lower())
        det_mock.assert_not_called()
        llm_mock.assert_not_called()
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_no_changes", events)
        self.assertNotIn("review_passed", events)

    def test_execute_one_blocks_ready_serial_without_serial_gate(self):
        issue = _make_issue("MAG-70")
        plan = IssuePlan(issue, "ready_serial", "Implementation work should start serially", ("dashboard/",), "medium")
        with tempfile.TemporaryDirectory() as tmp:
            with patch("supervisor.create_issue_worktree") as create_worktree:
                result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertFalse(result.ok)
        self.assertIn("Only low-risk ready_parallel", result.reason)
        create_worktree.assert_not_called()

    def test_execute_one_invokes_on_classification_blocked_hook(self):
        issue = _make_issue("MAG-47")
        plan = IssuePlan(issue, "needs_human", "Manual or deploy gate requires human review", ("summary/",), "high")
        captured = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("supervisor.create_issue_worktree") as create_worktree:
                result = execute_one_plan(
                    plan,
                    Path(tmp),
                    Path(tmp),
                    on_classification_blocked=lambda p, reason: captured.append((p.issue.identifier, reason)),
                )

        self.assertFalse(result.ok)
        create_worktree.assert_not_called()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "MAG-47")
        self.assertIn("Only low-risk ready_parallel", captured[0][1])

    def test_run_worker_with_heartbeat_emits_periodic_heartbeats(self):
        import time

        identifier = "MAG-99"

        def fake_run(*_args, **_kwargs):
            time.sleep(0.4)
            return CompletedProcess(["aider"], 0, "ok", "")

        buf = io.StringIO()
        with patch("supervisor.subprocess.run", side_effect=fake_run):
            with redirect_stdout(buf):
                completed = run_worker_with_heartbeat(
                    ["aider"],
                    cwd=Path("/tmp"),
                    timeout=10,
                    identifier=identifier,
                    interval_seconds=0.1,
                )

        self.assertEqual(completed.returncode, 0)
        events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        heartbeats = [e for e in events if e["event"] == "worker_heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertEqual(heartbeats[0]["identifier"], identifier)

    def test_worker_test_commands_dispatches_dashboard_node_test(self):
        plan = IssuePlan(_make_issue("MAG-68"), "ready_parallel", "low risk", ("dashboard/",), "low")
        commands = worker_test_commands(plan)
        self.assertTrue(any("node" in cmd and "--test" in cmd for cmd in commands))

    def test_worker_test_commands_dispatches_summary_bash_syntax_check(self):
        plan = IssuePlan(_make_issue("MAG-61"), "ready_parallel", "low risk", ("summary/",), "low")
        commands = worker_test_commands(plan)
        self.assertTrue(any("bash" in cmd and "-n" in cmd for cmd in commands))
        self.assertTrue(any("summary/symphony-summary.sh" in cmd for cmd in commands))

    def test_worker_test_commands_combines_multiple_path_families(self):
        plan = IssuePlan(_make_issue("MAG-99"), "ready_parallel", "low risk", ("dashboard/", "summary/"), "low")
        commands = worker_test_commands(plan)
        self.assertTrue(any("node" in cmd and "--test" in cmd for cmd in commands))
        self.assertTrue(any("bash" in cmd and "-n" in cmd for cmd in commands))

    def test_worker_test_commands_returns_empty_for_docs_only_plan(self):
        plan = IssuePlan(_make_issue("MAG-49"), "ready_parallel", "low risk", ("docs/",), "low")
        self.assertEqual(worker_test_commands(plan), ())

    def test_run_post_worker_tests_skips_when_no_commands(self):
        plan = IssuePlan(_make_issue("MAG-49"), "ready_parallel", "low risk", ("docs/",), "low")
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = run_post_worker_tests(plan, Path(tmp))
        self.assertTrue(result.ok)
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_tests_skipped", events)

    def test_run_post_worker_tests_passes_when_all_commands_exit_zero(self):
        plan = IssuePlan(_make_issue("MAG-68"), "ready_parallel", "low risk", ("dashboard/",), "low")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dashboard").mkdir()
            (Path(tmp) / "dashboard" / "symphony-overview.test.mjs").write_text("// stub")
            buf = io.StringIO()
            with patch(
                "supervisor.subprocess.run",
                return_value=CompletedProcess(["node"], 0, "ok", ""),
            ):
                with redirect_stdout(buf):
                    result = run_post_worker_tests(plan, Path(tmp))
        self.assertTrue(result.ok)
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_tests_passed", events)

    def test_run_post_worker_tests_fails_when_any_command_exits_nonzero(self):
        plan = IssuePlan(_make_issue("MAG-68"), "ready_parallel", "low risk", ("dashboard/",), "low")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dashboard").mkdir()
            (Path(tmp) / "dashboard" / "symphony-overview.test.mjs").write_text("// stub")
            buf = io.StringIO()
            with patch(
                "supervisor.subprocess.run",
                return_value=CompletedProcess(["node"], 1, "", "fetch failed"),
            ):
                with redirect_stdout(buf):
                    result = run_post_worker_tests(plan, Path(tmp))
        self.assertFalse(result.ok)
        self.assertIn("fetch failed", result.reason)
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_tests_failed", events)

    def test_run_post_worker_tests_skips_command_when_target_missing(self):
        # Cross-repo dispatch: predicted-path family registered (summary/), but
        # target script (summary/symphony-summary.sh) doesn't exist in the
        # dispatched repo's worktree (e.g. mc-briefings). Should NOT fail the
        # gate; should emit per-command worker_tests_skipped and pass overall.
        plan = IssuePlan(_make_issue("BRF-56"), "ready_serial", "low risk", ("summary/",), "low")
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch("supervisor.subprocess.run") as mock_run:
                with redirect_stdout(buf):
                    result = run_post_worker_tests(plan, Path(tmp))
            mock_run.assert_not_called()
        self.assertTrue(result.ok)
        self.assertIn("skipped", result.reason)
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("worker_tests_skipped", events)
        self.assertNotIn("worker_tests_failed", events)

    def test_execute_one_test_failure_blocks_llm_review(self):
        issue = _make_issue("MAG-68")
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("dashboard/",), "low")
        tests_fail = ReviewResult(False, "node --test exit 1: fetch failed", ("node --test dashboard/...",))
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                patch("supervisor.create_issue_worktree", return_value=Path(tmp)),
                patch(
                    "supervisor.subprocess.run",
                    return_value=CompletedProcess(["aider"], 0, "ready [symphony:done]", ""),
                ),
                patch("supervisor.capture_head_sha", side_effect=["start-sha", "end-sha"]),
                patch("supervisor.review_worktree", return_value=_DET_PASS),
                patch("supervisor.run_post_worker_tests", return_value=tests_fail),
                patch("supervisor.llm_review_diff") as llm_mock,
            ):
                with redirect_stdout(buf):
                    result = execute_one_plan(plan, Path(tmp), Path(tmp))

        self.assertFalse(result.ok)
        self.assertIn("node --test", result.reason)
        llm_mock.assert_not_called()
        events = [json.loads(line) for line in buf.getvalue().splitlines()]
        review_failed = [e for e in events if e["event"] == "review_failed"]
        self.assertTrue(review_failed)
        self.assertEqual(review_failed[0]["stage"], "tests")

    def test_execute_batch_passes_linear_hooks_to_each_worker(self):
        plans = (
            IssuePlan(_make_issue("MAG-52"), "ready_parallel", "low risk", ("docs/",), "low"),
            IssuePlan(_make_issue("MAG-53"), "ready_parallel", "low risk", ("summary/",), "low"),
        )
        batch = ParallelBatch("batch-1", plans)
        on_started = lambda *_args: None
        on_failed = lambda *_args: None
        on_passed = lambda *_args: None

        def fake_execute(plan, repo_root, worker_root, on_worker_started=None, on_worker_failed=None, on_review_passed=None):
            self.assertIs(on_worker_started, on_started)
            self.assertIs(on_worker_failed, on_failed)
            self.assertIs(on_review_passed, on_passed)
            return WorkerResult(plan.issue.identifier, True, "ok")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("supervisor.execute_one_plan", side_effect=fake_execute) as execute:
                results = execute_batch(
                    batch,
                    Path(tmp),
                    Path(tmp),
                    max_workers=2,
                    on_worker_started=on_started,
                    on_worker_failed=on_failed,
                    on_review_passed=on_passed,
                )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(sorted(result.identifier for result in results), ["MAG-52", "MAG-53"])
