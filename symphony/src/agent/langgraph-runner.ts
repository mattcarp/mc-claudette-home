import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { createInterface } from "node:readline";
import { execFile as execFileCallback } from "node:child_process";
import { promisify } from "node:util";
import { join, resolve, sep } from "node:path";
import { randomUUID } from "node:crypto";
import type { AgentEvent, AgentRunResult, RunOptions, Usage } from "./runner.js";
import type { WorkflowConfig, RunningSession } from "../types.js";

const DEFAULT_LANGGRAPH_SCRIPT = "symphony/src/agent/langgraph/main.py";
const execFile = promisify(execFileCallback);
const PLANNER_EVENTS = new Set([
  "model_route_selected",
  "issue_classified",
  "parallel_batch_planned",
  "worker_started",
  "worker_blocked",
  "worker_completed",
  "worker_failed",
  "worker_no_changes",
  "worker_heartbeat",
  "worker_tests_passed",
  "worker_tests_failed",
  "worker_tests_skipped",
  "deterministic_review_passed",
  "review_started",
  "review_failed",
  "review_passed",
  "parallel_batch_started",
  "parallel_batch_completed",
  "linear_comment_dry_run",
  "linear_comment_created",
  "linear_comment_failed",
  "linear_state_updated",
  "linear_state_update_failed",
  "linear_state_update_skipped",
  "auto_merge_succeeded",
  "auto_merge_failed",
  "auto_merge_skipped",
]);

const FAILURE_EVENT_TAGS = [
  "worker_blocked",
  "worker_failed",
  "worker_no_changes",
  "worker_tests_failed",
  "review_failed",
];

export function buildLanggraphIssueArgs({
  cfg,
  session,
  workerRoot,
}: {
  cfg: WorkflowConfig;
  session: RunningSession;
  workerRoot: string;
}): string[] {
  const teamKey = cfg.tracker.team_key;
  if (!teamKey) {
    throw new Error("langgraph_misconfigured: tracker.team_key is required for LangGraph issue execution");
  }
  return [
    "--plan-linear-team",
    teamKey,
    "--states",
    cfg.tracker.active_states.join(","),
    "--first",
    "100",
    "--max-workers",
    "1",
    "--execute-one",
    "--issue-id",
    session.identifier,
    "--worker-worktree-root",
    workerRoot,
    "--linear-comment-apply",
    "--allow-serial",
  ];
}

/**
 * A `worker_heartbeat` line proves the LangGraph child is still alive during a
 * silent model call (see `supervisor.py:run_worker_with_heartbeat`), but it is
 * NOT evidence the worker is making progress. The orchestrator's reconcile
 * watchdog uses `session.lastEventAt` to detect sessions stuck in
 * `LaunchingAgentProcess`, and a 30s heartbeat would otherwise mask every real
 * hang (model API stuck, OOM-thrashed aider, etc.).
 */
export function isHeartbeatLine(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed.startsWith("{")) return false;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && parsed.event === "worker_heartbeat";
  } catch {
    return false;
  }
}

export function shouldFinalizeLanggraphTurn(events: AgentEvent[]): boolean {
  return events.some(
    (event) =>
      event.kind === "other_message" &&
      event.type.includes("[LangGraph review_passed]"),
  );
}

export function shouldCreateDoneMarkerCommit(events: AgentEvent[]): boolean {
  return events.some(
    (event) =>
      event.kind === "other_message" &&
      (event.type.includes("[LangGraph auto_merge_succeeded]") ||
        (event.type.includes("[LangGraph linear_state_updated]") &&
          event.type.includes("state=Done"))),
  );
}

/**
 * Resolve the canonical mc-magpie checkout that hosts `symphony/.venv` and the
 * LangGraph python module.
 *
 * In production the orchestrator hands us `cwd=~/symphony_workspaces/<ISSUE>`
 * (a per-issue clone that does NOT have the venv), so operators must point us
 * at the canonical checkout via `LANGGRAPH_REPO_ROOT`. We fall back to the
 * `mc-magpie` segment of `cwd` for canary CLI usage.
 */
export function langgraphRepoRoot(cwd: string): string {
  const envRoot = process.env.LANGGRAPH_REPO_ROOT;
  if (envRoot && envRoot.trim().length > 0) return envRoot;
  return findRepoRoot(cwd);
}

export async function runLanggraphTurn(opts: RunOptions): Promise<AgentRunResult> {
  const { cwd, log, abort, session, cfg } = opts;
  const langgraphCfg = cfg.langgraph;

  if (!langgraphCfg) {
    return {
      ok: false,
      reason: "langgraph_misconfigured: langgraph config block missing in WORKFLOW.md",
      sessionId: null,
      usage: {},
      events: [],
    };
  }

  if (langgraphCfg.execution_mode !== "turn") {
    return {
      ok: false,
      reason:
        "langgraph_planner_only: set langgraph.execution_mode=turn before using LangGraph for issue turns",
      sessionId: null,
      usage: {},
      events: [],
    };
  }

  const repoRoot = langgraphRepoRoot(cwd);
  const venvPython = join(repoRoot, "symphony", ".venv", "bin", "python");
  const fullCommand = langgraphCfg.command ?? `${venvPython} ${DEFAULT_LANGGRAPH_SCRIPT}`;
  // Resolve relative components of the command against repoRoot so we can
  // spawn from a different cwd (cross-repo dispatch) without breaking paths
  // like "symphony/.venv/bin/python".
  const [cmdBinRaw, ...cmdArgsRaw] = fullCommand.split(" ");
  const cmdBin = cmdBinRaw && !cmdBinRaw.startsWith("/") ? join(repoRoot, cmdBinRaw) : cmdBinRaw;
  const cmdArgs = cmdArgsRaw.map((a) =>
    a && !a.startsWith("/") && (a.includes("/") || a.endsWith(".py")) ? join(repoRoot, a) : a,
  );
  // Worktree root is where Aider carves its working checkout. By default this
  // sits inside the LangGraph repo (mc-magpie), but for cross-repo dispatch
  // (running a different team's tickets in a sibling repo) operators set
  // LANGGRAPH_WORKER_WORKTREE_ROOT to the target repo's .langgraph-worktrees
  // path so worktrees are checkouts of the right git repo.
  const workerRootOverride = process.env.LANGGRAPH_WORKER_WORKTREE_ROOT?.trim();
  const workerRoot =
    workerRootOverride && workerRootOverride.length > 0
      ? workerRootOverride
      : join(repoRoot, ".langgraph-worktrees");
  // Spawn from the per-issue clone when LANGGRAPH_WORKER_WORKTREE_ROOT is set,
  // so `git worktree add` uses that target repo's git history rather than
  // mc-magpie's. Otherwise keep historical behavior (cwd=repoRoot).
  const spawnCwd =
    workerRootOverride && workerRootOverride.length > 0 ? cwd : repoRoot;
  let args: string[];
  try {
    args = [...cmdArgs, ...buildLanggraphIssueArgs({ cfg, session, workerRoot })];
  } catch (err) {
    return {
      ok: false,
      reason: String(err),
      sessionId: null,
      usage: {},
      events: [],
    };
  }

  const workerModel = process.env.LANGGRAPH_WORKER_MODEL || "deepseek/deepseek-v4-pro";
  const reviewerModel = process.env.LANGGRAPH_REVIEWER_MODEL || "openai/gpt-5.5";
  log.info("agent.spawn", {
    runner: "langgraph",
    cmd: `${cmdBin} ${args.join(" ")}`,
    model: `${workerModel}+${reviewerModel}`,
    cwd,
  });

  const synthSessionId = `langgraph-${randomUUID()}`;
  const events: AgentEvent[] = [];
  events.push({ kind: "session_started", sessionId: synthSessionId });
  session.sessionId = synthSessionId;
  log.info("agent.session_started", { runner: "langgraph", session_id: synthSessionId });

  let child: ChildProcessByStdio<Writable, Readable, Readable>;
  try {
    child = spawn(cmdBin, args, {
      cwd: spawnCwd,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (err) {
    return {
      ok: false,
      reason: `langgraph_not_found: ${String(err)}`,
      sessionId: synthSessionId,
      usage: {},
      events,
    };
  }
  child.stdin.end();

  let exitReason: string | null = null;
  let usage: Usage = {};

  const stallMs = langgraphCfg.stall_timeout_ms;
  const turnMs = langgraphCfg.turn_timeout_ms;

  let stallTimer: NodeJS.Timeout | undefined;
  const armStall = () => {
    if (stallMs <= 0) return;
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      exitReason = exitReason ?? "stalled";
      events.push({ kind: "stalled", elapsedMs: Date.now() - session.lastEventAt });
      log.warn("agent.stalled", {
        runner: "langgraph",
        stallMs,
        since_last_event_ms: Date.now() - session.lastEventAt,
      });
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 5000).unref();
    }, stallMs);
    stallTimer.unref();
  };
  armStall();

  const turnTimer = setTimeout(() => {
    exitReason = exitReason ?? "turn_timeout";
    log.warn("agent.turn_timeout", { runner: "langgraph", turnMs });
    child.kill("SIGTERM");
    setTimeout(() => child.kill("SIGKILL"), 5000).unref();
  }, turnMs);
  turnTimer.unref();

  const onAbort = () => {
    exitReason = exitReason ?? "canceled";
    child.kill("SIGTERM");
    setTimeout(() => child.kill("SIGKILL"), 5000).unref();
  };
  if (abort.aborted) onAbort();
  else abort.addEventListener("abort", onAbort, { once: true });

  const stderrChunks: string[] = [];
  const stderrRl = createInterface({ input: child.stderr, crlfDelay: Infinity });
  stderrRl.on("line", (line) => {
    if (stderrChunks.length < 200) stderrChunks.push(line);
  });

  const stdoutRl = createInterface({ input: child.stdout, crlfDelay: Infinity });
  stdoutRl.on("line", (line) => {
    if (!line.trim()) return;
    // Heartbeats prove the LangGraph child is still alive during a silent model
    // call (see `supervisor.py:run_worker_with_heartbeat`). By updating
    // `session.lastEventAt`, we let the orchestrator's reconcile watchdog know
    // the process is making progress and should not be killed as a stall.
    armStall();
    session.lastEventAt = Date.now();
    try {
      const parsed = JSON.parse(line);
      if (parsed.event === "node_started") {
        events.push({ kind: "other_message", type: `[Node ${parsed.node}] started` });
      } else if (parsed.event === "tool_use") {
        events.push({ kind: "tool_use", name: parsed.tool });
      } else if (parsed.event === "assistant_text") {
        events.push({ kind: "assistant_text", text: parsed.text });
      } else if (parsed.event === "usage") {
        usage = {
          input_tokens: (usage.input_tokens || 0) + (parsed.usage.input_tokens || 0),
          output_tokens: (usage.output_tokens || 0) + (parsed.usage.output_tokens || 0),
          total_tokens: (usage.total_tokens || 0) + (parsed.usage.total_tokens || 0),
        };
      } else if (parsed.event === "error") {
        events.push({ kind: "turn_failed", reason: parsed.error });
        log.warn("agent.langgraph_event", { event: "error", reason: parsed.error });
      } else if (PLANNER_EVENTS.has(parsed.event)) {
        events.push({ kind: "other_message", type: formatPlannerEvent(parsed) });
        // Log every LangGraph supervisor event into journalctl so operators can
        // see worker_no_changes / worker_tests_failed / review_failed reasons
        // without having to attach a debugger to the python child. Heartbeats
        // are deliberately info-level and short.
        const eventName = String(parsed.event);
        const isFailure = FAILURE_EVENT_TAGS.includes(eventName);
        const fields: Record<string, unknown> = { event: eventName };
        if (typeof parsed.identifier === "string") fields.identifier = parsed.identifier;
        if (typeof parsed.reason === "string") fields.reason = parsed.reason;
        if (typeof parsed.stage === "string") fields.stage = parsed.stage;
        if (typeof parsed.state === "string") fields.state = parsed.state;
        if (typeof parsed.pr_url === "string") fields.pr_url = parsed.pr_url;
        if (typeof parsed.elapsed_seconds === "number") fields.elapsed_seconds = parsed.elapsed_seconds;
        if (isFailure) log.warn("agent.langgraph_event", fields);
        else log.info("agent.langgraph_event", fields);
      }
    } catch {
      events.push({ kind: "assistant_text", text: line });
    }
  });

  const code: number = await new Promise((resolveExit) => {
    child.once("close", (c) => resolveExit(c ?? 0));
    child.once("error", () => resolveExit(1));
  });
  
  clearTimeout(stallTimer);
  clearTimeout(turnTimer);
  abort.removeEventListener("abort", onAbort);

  if (exitReason === "stalled") {
    return { ok: false, reason: "stalled", sessionId: synthSessionId, usage, events };
  }
  if (exitReason === "turn_timeout") {
    return { ok: false, reason: "turn_timeout", sessionId: synthSessionId, usage, events };
  }
  if (exitReason === "canceled") {
    return { ok: false, reason: "turn_cancelled", sessionId: synthSessionId, usage, events };
  }
  // Structured supervisor failure events take precedence over the bare exit
  // code so operators see e.g. "worker_no_changes" instead of "exit_code=1: ".
  const failure = langgraphFailureReason(events);
  if (failure) {
    return { ok: false, reason: failure, sessionId: synthSessionId, usage, events };
  }
  if (code !== 0) {
    return {
      ok: false,
      reason: `langgraph_exit_code=${code}: ${stderrChunks.slice(-10).join(" / ").slice(0, 500)}`,
      sessionId: synthSessionId,
      usage,
      events,
    };
  }
  if (!shouldFinalizeLanggraphTurn(events)) {
    return {
      ok: false,
      reason: "langgraph_no_review_passed",
      sessionId: synthSessionId,
      usage,
      events,
    };
  }

  if (shouldCreateDoneMarkerCommit(events)) {
    await createDoneMarkerCommit(cwd, session.identifier);
  }
  events.push({ kind: "turn_completed", usage, sessionId: synthSessionId });
  return { ok: true, sessionId: synthSessionId, usage, events };
}

export function langgraphFailureReason(events: AgentEvent[]): string | null {
  const failure = events.find(
    (event) =>
      event.kind === "turn_failed" ||
      (event.kind === "other_message" &&
        FAILURE_EVENT_TAGS.some((tag) => event.type.includes(`[LangGraph ${tag}]`))),
  );
  if (!failure) return null;
  if (failure.kind === "turn_failed") return failure.reason;
  if (failure.kind === "other_message") return failure.type;
  return "langgraph_failed";
}

async function createDoneMarkerCommit(cwd: string, identifier: string): Promise<void> {
  await execFile(
    "git",
    ["commit", "--allow-empty", "-m", `Finalize ${identifier}`, "-m", "[symphony:done]"],
    { cwd },
  );
}

function findRepoRoot(cwd: string): string {
  const resolved = resolve(cwd);
  const marker = `${sep}mc-magpie${sep}`;
  const idx = `${resolved}${sep}`.indexOf(marker);
  if (idx >= 0) return `${resolved}${sep}`.slice(0, idx + marker.length - 1);
  return resolved;
}

function formatPlannerEvent(parsed: Record<string, unknown>): string {
  const event = String(parsed.event ?? "langgraph_event");
  const identifier = typeof parsed.identifier === "string" ? ` ${parsed.identifier}` : "";
  const batch = typeof parsed.batch_id === "string" ? ` ${parsed.batch_id}` : "";
  const classification =
    typeof parsed.classification === "string" ? ` ${parsed.classification}` : "";
  const state = typeof parsed.state === "string" ? ` state=${parsed.state}` : "";
  let s = `[LangGraph ${event}]${identifier}${batch}${classification}${state}`.trim();
  const reason = typeof parsed.reason === "string" ? parsed.reason.trim() : "";
  if (reason) {
    const truncated = reason.length > 450 ? `${reason.slice(0, 450)}…` : reason;
    s += ` | ${truncated}`;
  }
  return s;
}
