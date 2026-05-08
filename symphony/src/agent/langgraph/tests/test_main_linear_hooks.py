import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import linear_execution_hooks, maybe_comment_needs_human, result_exit_code
from state import IssuePlan, LinearIssue
from state import WorkerResult


class FakeLinearClient:
    def __init__(self):
        self.transitions = []
        self.comments = []

    def transition_issue_to_state(self, team_key, issue_id, state_name):
        self.transitions.append((team_key, issue_id, state_name))
        return True

    def add_comment(self, issue_id, body):
        self.comments.append((issue_id, body))


class FakeCommentClient:
    def __init__(self):
        self.comments = []

    def add_comment(self, issue_id, body):
        self.comments.append((issue_id, body))


class MainLinearHookTests(unittest.TestCase):
    def test_execute_one_hooks_transition_todo_and_comment_ready_for_review(self):
        issue = LinearIssue(
            id="issue-1",
            identifier="MAG-50",
            title="docs: add runbook",
            description="",
            state="Todo",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        client = FakeLinearClient()
        on_started, _on_failed, on_passed, _on_blocked = linear_execution_hooks(client, "MAG")

        buf = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(buf):
            on_started(plan, Path("/tmp/worktree"))
            on_passed(plan, Path("/tmp/worktree"), SimpleNamespace(done_marker=False))

        self.assertEqual(client.transitions, [("MAG", "issue-1", "In Progress"), ("MAG", "issue-1", "In Review")])
        self.assertEqual(len(client.comments), 1)
        self.assertIn("ready for human review", client.comments[0][1])
        self.assertIn("Moved to In Review", client.comments[0][1])
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("linear_state_updated", events)
        self.assertIn("linear_comment_created", events)

    def test_execute_one_hooks_do_not_mark_done_when_done_marker_seen(self):
        issue = LinearIssue(
            id="issue-2",
            identifier="MAG-51",
            title="docs: add runbook",
            description="",
            state="In Progress",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        client = FakeLinearClient()
        _on_started, _on_failed, on_passed, _on_blocked = linear_execution_hooks(client, "MAG")

        with patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()):
            on_passed(plan, Path("/tmp/worktree"), SimpleNamespace(done_marker=True))

        self.assertEqual(client.transitions, [("MAG", "issue-2", "In Review")])
        self.assertIn("ready for human review", client.comments[0][1])
        self.assertIn("[symphony:done]", client.comments[0][1])

    def test_execute_one_hooks_mark_done_only_when_auto_done_enabled(self):
        issue = LinearIssue(
            id="issue-3",
            identifier="MAG-52",
            title="docs: add runbook",
            description="",
            state="In Progress",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        client = FakeLinearClient()
        _on_started, _on_failed, on_passed, _on_blocked = linear_execution_hooks(client, "MAG")

        with patch.dict("os.environ", {"LANGGRAPH_AUTO_DONE": "true"}, clear=True), redirect_stdout(io.StringIO()):
            on_passed(plan, Path("/tmp/worktree"), SimpleNamespace(done_marker=True))

        self.assertEqual(client.transitions, [("MAG", "issue-3", "Done")])
        self.assertIn("LANGGRAPH_AUTO_DONE", client.comments[0][1])

    def test_cli_accepts_allow_serial_flag(self):
        import main

        parser = main.build_parser()
        args = parser.parse_args(["--plan-linear-team", "MAG", "--execute-one", "--issue-id", "MAG-70", "--allow-serial"])

        self.assertTrue(args.allow_serial)

    def test_execute_one_does_not_comment_on_unrelated_needs_human_issues(self):
        issue = LinearIssue(
            id="issue-60",
            identifier="MAG-60",
            title="deploy dashboard",
            description="[symphony:deploy-ok]",
            state="Todo",
        )
        plan = IssuePlan(issue, "needs_human", "Manual or deploy gate requires human review", ("dashboard/",), "high")
        client = FakeCommentClient()
        args = SimpleNamespace(plan_linear_team="MAG", linear_comment_apply=True, execute_one=True, execute_batch=False)

        with redirect_stdout(io.StringIO()):
            maybe_comment_needs_human(args, [issue], [plan], client)

        self.assertEqual(client.comments, [])

    def test_result_exit_code_is_nonzero_when_any_worker_fails(self):
        self.assertEqual(result_exit_code([WorkerResult("MAG-68", True, "ok")]), 0)
        self.assertEqual(result_exit_code([WorkerResult("MAG-68", False, "worker failed")]), 1)

    def test_on_classification_blocked_transitions_to_in_review(self):
        issue = LinearIssue(
            id="issue-47",
            identifier="MAG-47",
            title="deploy film-style dailies summary",
            description="[symphony:deploy-ok]",
            state="Todo",
        )
        plan = IssuePlan(issue, "needs_human", "Manual or deploy gate requires human review", ("summary/",), "high")
        client = FakeLinearClient()
        _on_started, _on_failed, _on_passed, on_blocked = linear_execution_hooks(client, "MAG")

        buf = io.StringIO()
        with redirect_stdout(buf):
            on_blocked(plan, "Only low-risk ready_parallel issues can execute in this mode")

        self.assertEqual(client.transitions, [("MAG", "issue-47", "In Review")])
        self.assertEqual(len(client.comments), 1)
        self.assertIn("needs_human", client.comments[0][1])
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("linear_state_updated", events)

    def test_on_review_passed_auto_merges_when_env_enabled(self):
        issue = LinearIssue(
            id="issue-99",
            identifier="MAG-99",
            title="docs: tweak",
            description="",
            state="In Progress",
            labels=("auto-push",),
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        client = FakeLinearClient()
        _on_started, _on_failed, on_passed, _on_blocked = linear_execution_hooks(client, "MAG")

        with patch.dict("os.environ", {"LANGGRAPH_AUTO_MERGE": "true"}, clear=True):
            with patch("main.auto_merge_worktree") as mock_merge:
                mock_merge.return_value = (True, "https://github.com/x/y/pull/42")
                with redirect_stdout(io.StringIO()):
                    on_passed(plan, Path("/tmp/wt"), SimpleNamespace(done_marker=True))

        mock_merge.assert_called_once()
        # First positional arg is the plan
        self.assertIs(mock_merge.call_args.args[0], plan)
        # Linear comment should mention the merge URL
        self.assertEqual(len(client.comments), 1)
        self.assertIn("github.com", client.comments[0][1])

    def test_on_review_passed_skips_auto_merge_without_auto_push_label(self):
        issue = LinearIssue(
            id="issue-101",
            identifier="MAG-101",
            title="docs: tweak",
            description="",
            state="In Progress",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        client = FakeLinearClient()
        _on_started, _on_failed, on_passed, _on_blocked = linear_execution_hooks(client, "MAG")

        buf = io.StringIO()
        with patch.dict("os.environ", {"LANGGRAPH_AUTO_MERGE": "true"}, clear=True):
            with patch("main.auto_merge_worktree") as mock_merge:
                with redirect_stdout(buf):
                    on_passed(plan, Path("/tmp/wt"), SimpleNamespace(done_marker=True))

        mock_merge.assert_not_called()
        self.assertEqual(client.transitions, [("MAG", "issue-101", "In Review")])
        self.assertIn("missing the `auto-push` label", client.comments[0][1])
        events = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
        self.assertIn("auto_merge_skipped", events)

    def test_on_review_passed_skips_auto_merge_when_env_disabled(self):
        issue = LinearIssue(
            id="issue-100",
            identifier="MAG-100",
            title="docs: tweak",
            description="",
            state="In Progress",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        client = FakeLinearClient()
        _on_started, _on_failed, on_passed, _on_blocked = linear_execution_hooks(client, "MAG")

        with patch.dict("os.environ", {}, clear=True):
            with patch("main.auto_merge_worktree") as mock_merge:
                with redirect_stdout(io.StringIO()):
                    on_passed(plan, Path("/tmp/wt"), SimpleNamespace(done_marker=True))

        mock_merge.assert_not_called()
        self.assertEqual(client.transitions, [("MAG", "issue-100", "In Review")])

    def test_auto_merge_worktree_runs_push_pr_and_merge_in_order(self):
        from main import auto_merge_worktree
        issue = LinearIssue(
            id="issue-mag48",
            identifier="MAG-48",
            title="test(dashboard): smoke",
            description="",
            state="In Progress",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("dashboard/",), "low")
        from subprocess import CompletedProcess
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "branch" in cmd and "--show-current" in cmd:
                return CompletedProcess(cmd, 0, "langgraph/MAG-48-7\n", "")
            if cmd[:2] == ["git", "log"]:
                return CompletedProcess(cmd, 0, "test: smoke\n", "")
            if cmd[:2] == ["git", "push"]:
                return CompletedProcess(cmd, 0, "ok\n", "")
            if cmd[:2] == ["gh", "pr"] and cmd[2] == "create":
                return CompletedProcess(cmd, 0, "https://github.com/x/y/pull/42\n", "")
            if cmd[:2] == ["gh", "pr"] and cmd[2] == "merge":
                return CompletedProcess(cmd, 0, "merged\n", "")
            return CompletedProcess(cmd, 0, "", "")
        with patch("main.subprocess.run", side_effect=fake_run):
            ok, msg = auto_merge_worktree(plan, Path("/tmp/wt"))
        self.assertTrue(ok, msg)
        self.assertIn("github.com", msg)
        # Order: branch lookup, log title, push, pr create, pr merge
        first_words = [c[:3] for c in calls]
        self.assertEqual(first_words[0][:2], ["git", "branch"])
        self.assertIn(["git", "push", "origin"], [c[:3] for c in calls])
        self.assertIn(["gh", "pr", "create"], [c[:3] for c in calls])
        self.assertIn(["gh", "pr", "merge"], [c[:3] for c in calls])

    def test_auto_merge_worktree_returns_failure_when_push_fails(self):
        from main import auto_merge_worktree
        issue = LinearIssue(
            id="issue-mag48",
            identifier="MAG-48",
            title="t",
            description="",
            state="In Progress",
        )
        plan = IssuePlan(issue, "ready_parallel", "low risk", ("docs/",), "low")
        from subprocess import CompletedProcess
        def fake_run(cmd, **kw):
            if "branch" in cmd and "--show-current" in cmd:
                return CompletedProcess(cmd, 0, "langgraph/MAG-48-7\n", "")
            if cmd[:2] == ["git", "log"]:
                return CompletedProcess(cmd, 0, "subj\n", "")
            if cmd[:2] == ["git", "push"]:
                return CompletedProcess(cmd, 1, "", "remote rejected\n")
            return CompletedProcess(cmd, 0, "", "")
        with patch("main.subprocess.run", side_effect=fake_run):
            ok, msg = auto_merge_worktree(plan, Path("/tmp/wt"))
        self.assertFalse(ok)
        self.assertIn("push", msg.lower())

    def test_on_classification_blocked_skips_transition_when_already_in_review(self):
        issue = LinearIssue(
            id="issue-60",
            identifier="MAG-60",
            title="deploy gate",
            description="[symphony:deploy-ok]",
            state="In Review",
        )
        plan = IssuePlan(issue, "needs_human", "Manual or deploy gate requires human review", ("dashboard/",), "high")
        client = FakeLinearClient()
        _on_started, _on_failed, _on_passed, on_blocked = linear_execution_hooks(client, "MAG")

        with redirect_stdout(io.StringIO()):
            on_blocked(plan, "Only low-risk ready_parallel issues can execute in this mode")

        self.assertEqual(client.transitions, [])
        # No comment either when already In Review — avoid noise on retry loops
        self.assertEqual(client.comments, [])


if __name__ == "__main__":
    unittest.main()
