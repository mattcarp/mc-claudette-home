import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_router import DEFAULT_REVIEWER_MODEL, model_route


class ModelRouteTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            route = model_route()
        self.assertEqual(route.reviewer, DEFAULT_REVIEWER_MODEL)
        self.assertEqual(route.reviewer, "openai/gpt-5.5")

    def test_langgraph_model_does_not_bleed_into_reviewer(self):
        """LANGGRAPH_MODEL=deepseek/... must NOT override the reviewer default."""
        with patch.dict("os.environ", {"LANGGRAPH_MODEL": "deepseek/deepseek-v4-pro"}, clear=True):
            route = model_route()
        self.assertEqual(route.worker, "deepseek/deepseek-v4-pro")
        self.assertEqual(route.reviewer, DEFAULT_REVIEWER_MODEL)

    def test_langgraph_model_does_bleed_into_worker_and_planner(self):
        with patch.dict("os.environ", {"LANGGRAPH_MODEL": "deepseek/deepseek-v4-pro"}, clear=True):
            route = model_route()
        self.assertEqual(route.worker, "deepseek/deepseek-v4-pro")
        self.assertEqual(route.planner, "deepseek/deepseek-v4-pro")

    def test_explicit_reviewer_env_wins(self):
        with patch.dict(
            "os.environ",
            {
                "LANGGRAPH_MODEL": "deepseek/deepseek-v4-pro",
                "LANGGRAPH_REVIEWER_MODEL": "openai/gpt-4o",
            },
            clear=True,
        ):
            route = model_route()
        self.assertEqual(route.reviewer, "openai/gpt-4o")

    def test_explicit_worker_env_wins_over_base(self):
        with patch.dict(
            "os.environ",
            {
                "LANGGRAPH_MODEL": "deepseek/deepseek-v4-pro",
                "LANGGRAPH_WORKER_MODEL": "openai/gpt-4o-mini",
            },
            clear=True,
        ):
            route = model_route()
        self.assertEqual(route.worker, "openai/gpt-4o-mini")
