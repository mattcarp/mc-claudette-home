import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import run_allowed


class ToolTests(unittest.TestCase):
    def test_rejects_arbitrary_shell_strings(self):
        with self.assertRaises(RuntimeError):
            run_allowed(["bash", "-lc", "echo nope"], Path.cwd())
