# symphony/src/agent/langgraph

The LangGraph-based supervisor that classifies, dispatches, reviews, and (optionally) auto-merges Symphony work in parallel. Active in production.

This README is the source of truth for what's *actually here* as of 2026-05-08. It supersedes [`docs/superpowers/plans/2026-05-04-langgraph-parallel-supervisor.md`](../../../../docs/superpowers/plans/2026-05-04-langgraph-parallel-supervisor.md), which described the system as 11 unbuilt phases — a description that stopped matching reality some weeks ago. That plan has been retired.

## Status

**Production.** The TS runner integration in `../langgraph-runner.ts` is registered in `../registry.ts` as a first-class runner alongside `claude-code` and `aider`. At least one harness has flipped to LangGraph as the default (commit `dd68291`). Parallel-batch execution scaled to 6 concurrent (`c1f1d5b`). Auto-merge to main is gated by env var (`180f901`).

If you're an agent newly arriving here: do NOT treat the original plan doc's "phases" as backlog. Read this file, read recent commits with `git log --oneline symphony/src/agent/langgraph/`, ask Mattie what specific gap remains.

## File map

| File | Lines | Role |
|---|---|---|
| `state.py` | 61 | Frozen dataclasses + `GraphState` TypedDict. The domain types: `LinearIssue`, `IssuePlan`, `ParallelBatch`, `WorkerResult`, `ReviewResult`. |
| `events.py` | 10 | `emit(event, **payload)` — single-line JSON to stdout. Consumed by `../langgraph-runner.ts`'s line-by-line parser; the event names appear in `PLANNER_EVENTS` there. |
| `tools.py` | 28 | `run_allowed(command, cwd)` — narrow allowlist of shell commands the LLM can invoke (git status/diff/log, node --test, npx tsc). Refuses anything else. Closes the prompt-injection class for the supervisor's tool calls. |
| `model_router.py` | 27 | Env-var-driven model routing. `LANGGRAPH_PLANNER_MODEL` / `WORKER_MODEL` / `REVIEWER_MODEL`, with defaults `openai/gpt-4o` / `deepseek/deepseek-v4-pro` / `openai/gpt-5.5`. Reviewer intentionally doesn't inherit `LANGGRAPH_MODEL` fallback — a non-OpenAI base must not silently become the reviewer. |
| `linear_client.py` | 145 | `LinearGraphQLClient` for the live Linear queue. Filtered server-side by team key + state name. Also hosts `add_comment(issue_id, body)` for Phase-10 needs-human comments. |
| `planner.py` | 87 | Heuristic issue classifier: `ready_parallel` / `ready_serial` / `blocked` / `too_large` / `needs_human`. Plus `make_parallel_batches(plans, max_workers)` with conflict detection so two workers don't land on the same predicted-paths. |
| `reviewer.py` | 306 | `review_worktree(...)` — deterministic checks (status, diff non-empty, tests passing) THEN `llm_review_diff(...)` for an LLM gate. Strips `openai/` / `openrouter/` provider prefixes before reasoning-model match. |
| `worktree.py` | 59 | `create_issue_worktree(repo_root, identifier)` — git worktree at `.langgraph-worktrees-main/<TICKET>` on branch `langgraph/<TICKET>`. Handles retry suffixes `<TICKET>-2`, `<TICKET>-3`, ... |
| `supervisor.py` | 772 | The heart. `execute_one_plan` (single-worker, low-risk) and `execute_batch` (parallel via `ThreadPoolExecutor`). Spawns workers (default: `aider --model deepseek/deepseek-v4-pro`), captures heartbeats, runs the reviewer, optionally auto-merges. |
| `main.py` | 509 | CLI entry point + the LangGraph node graph (Coder / Reviewer / Supervisor / Tools). Auto-merge logic. Linear-comment dispatch for `needs_human` / `blocked` / `too_large` plans. Many flags. |

## Tests

`tests/` mirrors the modules: `test_events.py`, `test_linear_client.py`, `test_main_linear_hooks.py`, `test_model_router.py`, `test_planner.py`, `test_reviewer.py`, `test_supervisor.py`, `test_tools.py`, `test_worktree.py`. Plus fixtures under `fixtures/`. Run via the workshop Python toolchain; LangGraph + LangChain deps are pinned in `requirements.txt`.

## Execution flow (happy path)

```
main.py CLI args
    ↓
LinearGraphQLClient.fetch_active_issues(team_key)
    ↓
[planner.classify_issue(issue) for issue in issues]   →   IssuePlan list
    ↓
planner.make_parallel_batches(plans, max_workers)     →   ParallelBatch list
    ↓
supervisor.execute_batch(batch)
    │
    ├─→ worktree.create_issue_worktree() per IssuePlan
    │        emits "worker_started"
    │
    ├─→ ThreadPoolExecutor: spawn aider per worker
    │        emits "worker_heartbeat" during long runs
    │        captures stdout/stderr; SIGINT on stall
    │        emits "worker_completed" / "worker_failed" / "worker_no_changes"
    │
    ├─→ reviewer.review_worktree(worktree)
    │        deterministic: git status --short / diff --check / tests passing
    │        if all green: reviewer.llm_review_diff(...)
    │        emits "deterministic_review_passed" / "review_passed" / "review_failed"
    │
    └─→ (if LANGGRAPH_AUTO_MERGE=1 and review_passed)
             main.auto_merge_worktree(plan, worktree)
             git push → gh pr create → gh pr merge --squash
             emits "auto_merge_succeeded" / "auto_merge_failed"
```

The TS side (`../langgraph-runner.ts`) reads stdout line-by-line, parses each `{event: ...}` JSON, and forwards events into the orchestrator's structured-log pipeline + the dashboard.

## Environment variables

| Var | Default | Effect |
|---|---|---|
| `LANGGRAPH_MODEL` | (none) | Fallback for planner + worker if their specific vars aren't set. NOT inherited by reviewer. |
| `LANGGRAPH_PLANNER_MODEL` | `openai/gpt-4o` | Classifier model |
| `LANGGRAPH_WORKER_MODEL` | `deepseek/deepseek-v4-pro` | Default aider `--model` |
| `LANGGRAPH_REVIEWER_MODEL` | `openai/gpt-5.5` | LLM reviewer model. Provider prefixes stripped before reasoning-model match. |
| `LANGGRAPH_AUTO_DONE` | unset | When `1/true/yes/on`, supervisor adds `[symphony:done]` to the worker's commit if the reviewer passes |
| `LANGGRAPH_AUTO_MERGE` | unset | When `1/true/yes/on`, push branch + open PR + squash-merge to main on `review_passed` |
| `OPENAI_API_KEY` | required | OpenAI API. Used by planner, reviewer (for OpenAI models). |
| `OPENROUTER_API_KEY` | optional | If set, reviewer can use OpenRouter-hosted models (commit `7cd3b4b`) |
| `LINEAR_API_KEY` | required | Linear GraphQL |

Per-harness WORKFLOW.md selects this runner via `agent: { runner: langgraph }`.

## Worktrees on disk

Two roots, two patterns:

- `.langgraph-worktrees-main/<TICKET>` — workshop's main supervisor scratch space. Retries get suffixed `<TICKET>-2`, `<TICKET>-3`, ...
- `~/.config/superpowers/worktrees/mc-magpie/langgraph-live-workers/<TICKET>` — live workers that are part of an in-flight feature batch.

`git worktree list` is the truth. If a worktree is wedged or stale, `git worktree remove --force` then re-run; `worktree.py` is idempotent on creation.

## What's wired but worth knowing about

- **Parking heuristic.** Issues that hit `worker_no_changes` repeatedly get *parked* in Linear (label/state change) so the supervisor stops retrying them. Maintenance scripts under `../../../../scripts/`: `audit_failures.mjs`, `unpark_failed_done.mjs`, `cad-cleanup.mjs`. (Commit `78a52cc`.)
- **Pre-dispatch shell hook.** Optional `cfg.langgraph.pre_dispatch_shell` runs before each batch; if it exits with 77, the batch aborts cleanly. Lets the operator gate dispatch on external state. (Commit `78a52cc`.)
- **Reviewer quota pause.** When the reviewer model hits a 429 / quota error, dispatch is paused for the rest of the cycle rather than burning credits on a guaranteed-failing review. (Commit `9be5eb2`.)
- **Cross-repo dispatch.** A single LangGraph supervisor can work across multiple project repos (e.g. CAD tickets dispatch to mc-cadence, BRF to mc-briefings) with per-repo worktrees. (Commit `356831b`.)
- **Shell-shaped path guard.** Worker patches that add new paths with shell-metacharacter-shaped names are rejected at review time — defense in depth against prompt-injected `path = "; rm -rf ~"` style additions. (Commit `ac70ef9`.)

## Operating

### Kill switches

```bash
# 1. Stop auto-merging review_passed branches; supervisor still runs:
unset LANGGRAPH_AUTO_MERGE
# (or in WORKFLOW.md, drop the env entry)

# 2. Stop the LangGraph runner entirely; harness falls back to claude-code / aider:
# In WORKFLOW.md, change `agent.runner: langgraph` to `agent.runner: claude-code`.
# Restart the harness.

# 3. Halt all symphony harnesses on workshop:
ssh sysop@workshop systemctl --user stop 'symphony-*.service'
```

### Where to look when something's off

| Symptom | First place |
|---|---|
| Workers spinning forever | `git worktree list` — count of `langgraph/*` worktrees should be ≤ `max_workers` |
| "review_passed" but no merge | Is `LANGGRAPH_AUTO_MERGE` set? `gh auth status`? Is the branch protection rejecting the squash? |
| Same issue retried indefinitely | The parking heuristic should kick in; if it isn't, `audit_failures.mjs` against the team's recent run history |
| Reviewer model errors | Check `LANGGRAPH_REVIEWER_MODEL` matches what's actually deployed; provider-prefix stripping bugs have bitten before (commit `7c3170f`) |
| Cross-repo dispatch hits wrong repo | `team-keys.json` in `planner/` is the project ↔ team mapping; supervisor uses the same mapping |

### Adding a new model

1. Set `LANGGRAPH_*_MODEL` to the new identifier (e.g. `anthropic/claude-opus-4-5` via OpenRouter).
2. Verify token-usage parsing in `main.py:84-93` — different providers shape `response_metadata.token_usage` differently, and silent zeros here cause the dashboard to misreport spend.
3. Run a single-issue execute through `main.py --execute-one --identifier MAG-XXX --dry-run` against a fixture before letting it touch a real ticket.

## Known follow-ups (non-exhaustive)

These are the kinds of things Mattie has expressed mild irritation about; not necessarily formal backlog items but worth knowing if you're in here:

- The Coder / Reviewer / Supervisor / Tools graph in `main.py` is a leftover from an earlier prototype shape; the actual production path goes through `supervisor.execute_batch` directly. The graph still works but is largely vestigial.
- `tests/` coverage on `supervisor.py` is thinner than the file warrants — it's the largest module and the most-touched in recent fixes.
- `model_router.py`'s default of `gpt-4o` for the planner predates GPT-5.x being widely available; bump consideration when convenient.

If you're tempted to start "the LangGraph project" cold from the original plan doc: stop. Read this file. Read recent commits. Ask Mattie. The old plan would have you re-implement two months of shipped work.
