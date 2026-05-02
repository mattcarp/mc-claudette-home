# mc-symphony

Long-running harness that polls Linear and dispatches Claude Code sessions per issue. Implements the [OpenAI Symphony spec](https://github.com/openai/symphony/blob/main/SPEC.md) with two substitutions:

- **Linear** for the tracker (same as upstream)
- **Claude Code** (headless, `claude -p --output-format=stream-json`) for the coding agent — replacing OpenAI's `codex app-server`

## Why

Mostly: a supervised, restart-resilient loop is a better shape for unattended agent work than fire-and-forget calls. The orchestrator owns time (poll cadence, stall detection, retry backoff). The agent owns the work. State is intentionally not persisted — recovery is tracker-driven and filesystem-driven.

## Layout

```
symphony/
├── WORKFLOW.md              YAML config + Liquid prompt template
├── src/
│   ├── main.ts              entrypoint, signal handling, optional HTTP server
│   ├── workflow-loader.ts   parses front-matter + body, resolves $ENV refs
│   ├── tracker/linear.ts    Linear GraphQL: candidates, by-id state, by-id full
│   ├── workspace.ts         per-issue dirs, sanitized keys, hook execution
│   ├── agent/runner.ts      spawns Claude Code, parses stream-json, stall/turn timeouts
│   ├── orchestrator/        poll loop, claims, dispatch, retry queue, reconciliation
│   ├── server.ts            optional /api/v1/state + dashboard
│   ├── logger.ts            structured key=value logs
│   └── types.ts             zod schemas + shared types
```

## Setup

Workshop (production runtime):

```bash
ssh sysop@workshop
cd ~/projects/mc-briefings/symphony
npm install
npm i -g @anthropic-ai/claude-code        # if not present
export LINEAR_API_KEY=lin_api_...
export LINEAR_PROJECT_SLUG=mc-briefings-...
npm start
```

Local dev:

```bash
cd symphony
npm install
LINEAR_API_KEY=... LINEAR_PROJECT_SLUG=... npm run dev
```

## Operation

Once running, the loop is autonomous:

1. Every `polling.interval_ms`, fetch Linear issues in `active_states` for the configured project.
2. Sort by priority, then created_at, then identifier.
3. For each eligible issue (not already claimed, slot available, no live blockers if `Todo`), claim and dispatch.
4. Per dispatch: ensure `~/symphony_workspaces/<ID>/` exists, run `after_create` hook on first creation, run `before_run` hook every turn, spawn Claude in that directory.
5. Stream the agent's JSON events. If `stall_timeout_ms` elapses without an event, kill and queue retry. If turn completes, refresh issue state from Linear; if still active and `turn_count < max_turns`, take another turn on the same session via `--resume`.
6. On worker exit, schedule a 1-second continuation re-check. Failed runs use exponential backoff (`10s * 2^(n-1)`, capped at `max_retry_backoff_ms`).
7. On terminal state (Done/Canceled), release claim and clean workspace.

## Operator surface

If `server.port` is set in WORKFLOW.md (default 4747 in the sample):

- `GET http://127.0.0.1:4747/` — HTML dashboard with running and retrying sessions
- `GET http://127.0.0.1:4747/api/v1/state` — JSON snapshot

## Safety

- Strict tool allowlist in `codex.command` — Claude can read/edit project files and run a curated set of git/npm commands. No `Bash(*)`, no `--dangerously-skip-permissions`.
- Workspace path is validated to live under `workspace.root` before agent launch.
- `$VAR` indirection in WORKFLOW.md keeps secrets out of the file.
- Hooks run with timeout enforced (default 60s, override via `hooks.timeout_ms`).
- This harness does not push to GitHub by default. Add `auto-push` label to an issue to allow it (logic is in `WORKFLOW.md` prompt — agent honors it).

## What's not implemented yet

- Dynamic `WORKFLOW.md` reload (restart to apply config changes)
- `linear_graphql` client-side tool extension (Claude can use the Linear MCP server instead)
- Per-state concurrency caps (only global cap currently)
