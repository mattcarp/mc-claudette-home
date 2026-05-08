from __future__ import annotations

from state import IssuePlan, LinearIssue, ParallelBatch


def classify_issue(issue: LinearIssue) -> IssuePlan:
    text = f"{issue.title}\n{issue.description}".lower()
    labels = {label.lower() for label in issue.labels}
    predicted = predict_paths(issue)

    if issue.blocked_by:
        return IssuePlan(issue, "blocked", "Issue is blocked by unresolved dependencies", predicted, "medium")

    if "manual-only" in labels or "deploy" in labels or "[symphony:deploy-ok]" in text:
        return IssuePlan(issue, "needs_human", "Manual or deploy gate requires human review", predicted, "high")

    if any(word in text for word in ("architecture", "refactor", "full-blown", "migration")):
        return IssuePlan(issue, "too_large", "Likely larger than one safe worker turn", predicted, "high")

    if labels & {"docs", "test"} or text.startswith(("docs", "test")):
        return IssuePlan(issue, "ready_parallel", "Docs/test work is low risk", predicted, "low")

    return IssuePlan(issue, "ready_serial", "Implementation work should start serially", predicted, "medium")


def make_parallel_batches(plans: list[IssuePlan], max_workers: int) -> list[ParallelBatch]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    runnable = [p for p in plans if p.classification == "ready_parallel"]
    batches: list[list[IssuePlan]] = []

    for plan in runnable:
        placed = False
        for batch in batches:
            if len(batch) >= max_workers:
                continue
            if conflicts_with_batch(plan, batch):
                continue
            batch.append(plan)
            placed = True
            break
        if not placed:
            batches.append([plan])

    return [
        ParallelBatch(batch_id=f"batch-{idx + 1}", plans=tuple(batch))
        for idx, batch in enumerate(batches)
    ]


def conflicts_with_batch(plan: IssuePlan, batch: list[IssuePlan] | tuple[IssuePlan, ...]) -> bool:
    paths = set(plan.predicted_paths)
    return any(paths & set(other.predicted_paths) for other in batch)


def batch_has_conflicts(batch: ParallelBatch) -> bool:
    seen: set[str] = set()
    for plan in batch.plans:
        paths = set(plan.predicted_paths)
        if seen & paths:
            return True
        seen.update(paths)
    return False


def predict_paths(issue: LinearIssue) -> tuple[str, ...]:
    text = f"{issue.title}\n{issue.description}\n{' '.join(issue.labels)}".lower()
    paths: list[str] = []
    if "dashboard" in text:
        paths.append("dashboard/")
    if "summary" in text:
        paths.append("summary/")
    if "screencast" in text:
        paths.append("screencast/")
    if "planner" in text or "linear" in text:
        paths.append("planner/")
    if "symphony" in text:
        paths.append("symphony/")
    if "mac-mini" in text or "macbook-air" in text or "launchagent" in text:
        paths.append("mac-mini/")
        paths.append("macbook-air/")
    if "docs" in text or text.startswith("docs"):
        paths.append("docs/")
    if "test" in text or text.startswith("test"):
        paths.append("tests/")
    return tuple(paths or ("unknown/",))
