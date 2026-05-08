from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRoute:
    planner: str
    worker: str
    reviewer: str


DEFAULT_PLANNER_MODEL = "openai/gpt-4o"
DEFAULT_WORKER_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_REVIEWER_MODEL = "openai/gpt-5.5"


def model_route() -> ModelRoute:
    base = os.environ.get("LANGGRAPH_MODEL")
    # reviewer intentionally does NOT inherit LANGGRAPH_MODEL fallback: a
    # non-OpenAI base model must not silently become the reviewer model.
    return ModelRoute(
        planner=os.environ.get("LANGGRAPH_PLANNER_MODEL", base or DEFAULT_PLANNER_MODEL),
        worker=os.environ.get("LANGGRAPH_WORKER_MODEL", base or DEFAULT_WORKER_MODEL),
        reviewer=os.environ.get("LANGGRAPH_REVIEWER_MODEL") or DEFAULT_REVIEWER_MODEL,
    )
