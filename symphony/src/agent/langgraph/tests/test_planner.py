import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner import classify_issue, make_parallel_batches
from state import LinearIssue, IssuePlan


class StateModelTests(unittest.TestCase):
    def test_issue_plan_holds_classification(self):
        issue = LinearIssue(
            id="issue-id",
            identifier="MAG-48",
            title="test(dashboard): add smoke test",
            description="Add health checks",
            state="Todo",
            labels=("dashboard", "test"),
        )
        plan = IssuePlan(
            issue=issue,
            classification="ready_parallel",
            reason="docs or tests only",
            predicted_paths=("dashboard/",),
            risk="low",
        )

        self.assertEqual(plan.issue.identifier, "MAG-48")
        self.assertEqual(plan.classification, "ready_parallel")


class PlannerTests(unittest.TestCase):
    def issue(self, identifier: str, title: str, labels=(), description="", blocked_by=()):
        return LinearIssue(
            id=identifier.lower(),
            identifier=identifier,
            title=title,
            description=description,
            state="Todo",
            labels=tuple(labels),
            blocked_by=tuple(blocked_by),
        )

    def test_classifies_blocked_issue(self):
        plan = classify_issue(self.issue("MAG-1", "feat: add thing", blocked_by=("MAG-0",)))
        self.assertEqual(plan.classification, "blocked")
        self.assertIn("blocked", plan.reason.lower())

    def test_classifies_docs_as_parallel(self):
        plan = classify_issue(self.issue("MAG-2", "docs(dashboard): write runbook", labels=("docs", "dashboard")))
        self.assertEqual(plan.classification, "ready_parallel")
        self.assertEqual(plan.risk, "low")
        self.assertIn("docs/", plan.predicted_paths)

    def test_classifies_dashboard_code_as_serial_when_same_file_likely(self):
        plan = classify_issue(self.issue("MAG-3", "feat(dashboard): add filters", labels=("dashboard", "ux")))
        self.assertEqual(plan.classification, "ready_serial")
        self.assertIn("dashboard/", plan.predicted_paths)

    def test_batches_non_overlapping_paths(self):
        docs = classify_issue(self.issue("MAG-4", "docs(summary): write runbook", labels=("docs",)))
        tests = classify_issue(self.issue("MAG-5", "test(dashboard): add aggregate tests", labels=("test",)))
        batches = make_parallel_batches([docs, tests], max_workers=4)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0].plans), 2)

    def test_separates_overlapping_paths(self):
        a = classify_issue(self.issue("MAG-6", "test(dashboard): add filters coverage", labels=("dashboard", "test")))
        b = classify_issue(self.issue("MAG-7", "docs(dashboard): add refresh controls", labels=("dashboard", "docs")))
        batches = make_parallel_batches([a, b], max_workers=4)
        self.assertGreaterEqual(len(batches), 2)
