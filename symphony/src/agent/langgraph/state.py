from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict


IssueClass = Literal["ready_parallel", "ready_serial", "blocked", "too_large", "needs_human"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    state: str
    labels: tuple[str, ...] = ()
    priority: int | None = None
    blocked_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssuePlan:
    issue: LinearIssue
    classification: IssueClass
    reason: str
    predicted_paths: tuple[str, ...] = ()
    risk: RiskLevel = "medium"


@dataclass(frozen=True)
class ParallelBatch:
    batch_id: str
    plans: tuple[IssuePlan, ...]


@dataclass(frozen=True)
class WorkerResult:
    identifier: str
    ok: bool
    reason: str
    worktree: str | None = None
    done_marker: bool = False


@dataclass(frozen=True)
class ReviewResult:
    identifier: str
    ok: bool
    reason: str


class GraphState(TypedDict, total=False):
    team_key: str
    issues: list[LinearIssue]
    plans: list[IssuePlan]
    batches: list[ParallelBatch]
    worker_results: list[WorkerResult]
    review_results: list[ReviewResult]
    errors: list[str]
