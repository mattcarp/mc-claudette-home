import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worktree import branch_name_for_issue, next_available_worktree_target, safe_worktree_name


class WorktreeTests(unittest.TestCase):
    def test_safe_worktree_name(self):
        self.assertEqual(safe_worktree_name("MAG-48"), "mag-48")
        self.assertEqual(safe_worktree_name("MAG 48 weird"), "mag-48-weird")

    def test_branch_name_for_issue(self):
        self.assertEqual(branch_name_for_issue("MAG-48"), "langgraph/MAG-48")

    def test_next_available_worktree_target_skips_existing_branches(self):
        def branch_exists(_repo_root, branch):
            return branch in {"langgraph/MAG-48", "langgraph/MAG-48-2"}

        with patch("worktree._branch_exists", side_effect=branch_exists):
            path, branch = next_available_worktree_target(
                Path("/repo"),
                "MAG-48",
                Path("/repo/.langgraph-worktrees"),
            )

        self.assertEqual(path, Path("/repo/.langgraph-worktrees/mag-48-3"))
        self.assertEqual(branch, "langgraph/MAG-48-3")
