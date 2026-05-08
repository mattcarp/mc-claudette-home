import { describe, expect, it } from "vitest";
import type { WorkflowConfig, RunningSession } from "../types.js";
import {
  buildLanggraphIssueArgs,
  isHeartbeatLine,
  langgraphFailureReason,
  langgraphRepoRoot,
  shouldCreateDoneMarkerCommit,
  shouldFinalizeLanggraphTurn,
} from "./langgraph-runner.js";

function config(overrides: Partial<WorkflowConfig> = {}): WorkflowConfig {
  return {
    tracker: {
      kind: "linear",
      api_key: "linear-key",
      team_key: "MAG",
      active_states: ["Todo", "In Progress"],
      terminal_states: ["Done"],
      exclude_labels: [],
      require_any_labels: [],
    },
    posture: "greenfield",
    polling: { interval_ms: 30_000 },
    workspace: { root: "/tmp/symphony-test" },
    hooks: { timeout_ms: 60_000 },
    agent: {
      max_concurrent_agents: 1,
      max_turns: 3,
      max_retry_backoff_ms: 60_000,
      runner: "langgraph",
      fallback: [],
    },
    codex: {
      command: "claude -p",
      turn_timeout_ms: 60_000,
      read_timeout_ms: 1_000,
      stall_timeout_ms: 30_000,
    },
    langgraph: {
      command: "python symphony/src/agent/langgraph/main.py",
      turn_timeout_ms: 60_000,
      stall_timeout_ms: 30_000,
      max_workers: 1,
      execution_mode: "turn",
      max_consecutive_worker_no_changes: 3,
    },
    server: { host: "127.0.0.1", port: 4754 },
    ...overrides,
  };
}

function session(identifier = "MAG-68"): RunningSession {
  return {
    issueId: "issue-id",
    identifier,
    workspacePath: "",
    startedAt: Date.now(),
    lastEventAt: Date.now(),
    turnCount: 0,
    phase: "LaunchingAgentProcess",
    sessionId: null,
    abort: new AbortController(),
    totals: { input: 0, output: 0, total: 0 },
  };
}

describe("LangGraph runner integration", () => {
  it("builds args for the real Linear planner/supervisor execute-one path", () => {
    const args = buildLanggraphIssueArgs({
      cfg: config(),
      session: session("MAG-68"),
      workerRoot: ".langgraph-worktrees",
    });

    expect(args).toContain("--plan-linear-team");
    expect(args).toContain("MAG");
    expect(args).toContain("--execute-one");
    expect(args).toContain("--issue-id");
    expect(args).toContain("MAG-68");
    expect(args).toContain("--allow-serial");
    expect(args).toContain("--linear-comment-apply");
    expect(args).not.toContain("--prompt-file");
  });

  it("requires a Linear team key for issue execution", () => {
    const cfg = config({
      tracker: {
        kind: "linear",
        api_key: "linear-key",
        active_states: ["Todo"],
        terminal_states: ["Done"],
        exclude_labels: [],
        require_any_labels: [],
      },
    });

    expect(() =>
      buildLanggraphIssueArgs({
        cfg,
        session: session(),
        workerRoot: ".langgraph-worktrees",
      }),
    ).toThrow(/team_key/);
  });

  it("finalizes the orchestrator turn only after LangGraph review passes", () => {
    expect(shouldFinalizeLanggraphTurn([{ kind: "other_message", type: "[LangGraph review_passed] MAG-68" }])).toBe(true);
    expect(shouldFinalizeLanggraphTurn([{ kind: "other_message", type: "[LangGraph review_failed] MAG-68" }])).toBe(false);
  });

  it("creates the done marker only after auto-merge or explicit Done transition", () => {
    expect(shouldCreateDoneMarkerCommit([{ kind: "other_message", type: "[LangGraph review_passed] MAG-68" }])).toBe(false);
    expect(shouldCreateDoneMarkerCommit([{ kind: "other_message", type: "[LangGraph auto_merge_succeeded] MAG-68" }])).toBe(true);
    expect(shouldCreateDoneMarkerCommit([{ kind: "other_message", type: "[LangGraph linear_state_updated] MAG-68 state=Done" }])).toBe(true);
    expect(shouldCreateDoneMarkerCommit([{ kind: "other_message", type: "[LangGraph linear_state_updated] MAG-68 state=In Review" }])).toBe(false);
  });

  it("prefers LANGGRAPH_REPO_ROOT over the orchestrator workspace cwd", () => {
    const original = process.env.LANGGRAPH_REPO_ROOT;
    process.env.LANGGRAPH_REPO_ROOT = "/home/sysop/projects/mc-magpie";
    try {
      expect(langgraphRepoRoot("/home/sysop/symphony_workspaces/MAG-68")).toBe(
        "/home/sysop/projects/mc-magpie",
      );
    } finally {
      if (original === undefined) delete process.env.LANGGRAPH_REPO_ROOT;
      else process.env.LANGGRAPH_REPO_ROOT = original;
    }
  });

  it("surfaces worker_no_changes as the failure reason when present", () => {
    const reason = langgraphFailureReason([
      { kind: "other_message", type: "[LangGraph worker_started] MAG-50" },
      { kind: "other_message", type: "[LangGraph worker_no_changes] MAG-50 start=abc end=abc" },
    ]);
    expect(reason).toBeTruthy();
    expect(reason).toContain("worker_no_changes");
  });

  it("surfaces worker_tests_failed as the failure reason when present", () => {
    const reason = langgraphFailureReason([
      { kind: "other_message", type: "[LangGraph worker_completed] MAG-49" },
      { kind: "other_message", type: "[LangGraph worker_tests_failed] MAG-49" },
    ]);
    expect(reason).toBeTruthy();
    expect(reason).toContain("worker_tests_failed");
  });

  it("surfaces review_failed with a stage prefix as the failure reason", () => {
    const reason = langgraphFailureReason([
      { kind: "other_message", type: "[LangGraph review_failed] MAG-68" },
    ]);
    expect(reason).toBeTruthy();
    expect(reason).toContain("review_failed");
  });

  it("identifies worker_heartbeat lines so they don't mask real stalls", () => {
    expect(isHeartbeatLine('{"event":"worker_heartbeat","identifier":"MAG-37","elapsed_seconds":540}')).toBe(true);
    expect(isHeartbeatLine('  {"event":"worker_heartbeat","elapsed_seconds":30}\n')).toBe(true);
    expect(isHeartbeatLine('{"event":"worker_started","identifier":"MAG-37"}')).toBe(false);
    expect(isHeartbeatLine('{"event":"tool_use","tool":"git"}')).toBe(false);
    expect(isHeartbeatLine("plain assistant prose, not JSON")).toBe(false);
    expect(isHeartbeatLine("")).toBe(false);
    expect(isHeartbeatLine("{not valid json")).toBe(false);
  });

  it("falls back to deriving the repo root from cwd when LANGGRAPH_REPO_ROOT is unset", () => {
    const original = process.env.LANGGRAPH_REPO_ROOT;
    delete process.env.LANGGRAPH_REPO_ROOT;
    try {
      expect(langgraphRepoRoot("/Users/matt/Documents/projects/mc-magpie/.langgraph-worktrees/mag-68")).toBe(
        "/Users/matt/Documents/projects/mc-magpie",
      );
    } finally {
      if (original !== undefined) process.env.LANGGRAPH_REPO_ROOT = original;
    }
  });
});
