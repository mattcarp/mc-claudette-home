import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events import emit


class EventTests(unittest.TestCase):
    def test_emit_writes_one_json_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit("issue_classified", identifier="MAG-48", classification="ready_parallel")

        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["event"], "issue_classified")
        self.assertEqual(parsed["identifier"], "MAG-48")
        self.assertEqual(parsed["classification"], "ready_parallel")
