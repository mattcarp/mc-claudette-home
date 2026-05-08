import { describe, expect, it, vi } from "vitest";
import { Orchestrator, isLanggraphWorkerNoChangesFailure } from "./orchestrator.js";
import type { Issue, Workflow } from "../types.js";
import type { Logger } from "../logger.js";
import { LinearClient } from "../tracker/linear.js";
import * as processPressure from "../process-pressure.js";

const logger: Logger = {
  info: vi.fn(),
  warn: vi.fn(),
  error: vi.fn(),
  debug: vi.fn(),
  child: () => logger,
};

function workflow(): Workflow {
  return {
    sourcePath: "WORKFLOW.md",
    promptTemplate: "prompt",
    config: {
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
        runner: "claude-code",
        fallback: [],
      },
      codex: {
        command: "claude -p",
        turn_timeout_ms: 60_000,
        read_timeout_ms: 1_000,
        stall_timeout_ms: 30_000,
      },
      server: { host: "127.0.0.1", port: 4754 },
    },
  };
}

function issue(): Issue {
  return {
    id: "issue-id",
    identifier: "MAG-32",
    title: "Fix fallback",
    description: null,
    state: "In Progress",
    priority: 1,
    labels: [],
    blocked_by: [],
    url: "https://linear.app/test/issue/MAG-32",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

describe("isLanggraphWorkerNoChangesFailure", () => {
  it("matches LangGraph empty-diff style reasons", () => {
    expect(isLanggraphWorkerNoChangesFailure("worker_no_changes")).toBe(true);
    expect(isLanggraphWorkerNoChangesFailure("LangGraph failed: worker_no_changes")).toBe(true);
    expect(isLanggraphWorkerNoChangesFailure("no new commit after worker")).toBe(true);
    expect(isLanggraphWorkerNoChangesFailure("HEAD unchanged")).toBe(true);
    expect(isLanggraphWorkerNoChangesFailure("empty diff")).toBe(true);
  });

  it("returns false for unrelated failures", () => {
    expect(isLanggraphWorkerNoChangesFailure("tests failed")).toBe(false);
    expect(isLanggraphWorkerNoChangesFailure("review_failed")).toBe(false);
    expect(isLanggraphWorkerNoChangesFailure("")).toBe(false);
  });
});

describe("Orchestrator retries", () => {
  it("keeps failure attempts across retry dispatches so Telegram only gets the first failure", () => {
    const orchestrator = new Orchestrator(workflow(), logger);
    const failed = vi.fn();
    const notifier = { started: vi.fn(), done: vi.fn(), failed };
    const internals = orchestrator as unknown as {
      notifier: typeof notifier;
      retries: Map<string, unknown>;
      scheduleRetry(issue: Issue, reason: string): void;
    };
    internals.notifier = notifier;

    internals.scheduleRetry(issue(), "first failure");
    const firstSnapshot = orchestrator.snapshot().retrying[0];
    internals.retries.delete("issue-id");
    internals.scheduleRetry(issue(), "second failure");
    const secondSnapshot = orchestrator.snapshot().retrying[0];

    expect(firstSnapshot?.attempt).toBe(1);
    expect(secondSnapshot?.attempt).toBe(2);
    expect(failed).toHaveBeenCalledTimes(1);
  });
});

describe("Orchestrator reviewer quota cooldown", () => {
  it("skips dispatch and does not call Linear or process guard while cooldown is active", async () => {
    const fetchSpy = vi.spyOn(LinearClient.prototype, "fetchCandidateIssues").mockResolvedValue([]);
    const pressureSpy = vi.spyOn(processPressure, "readProcessPressure").mockResolvedValue({
      checkedAt: Date.now(),
      load1: 0,
      freeMemMb: 8192,
      riskyProcessCount: 0,
      riskyProcessSample: [],
      maxLoad1: 128,
      minFreeMemMb: 2048,
      maxRiskyProcesses: 6,
      ok: true,
      reason: null,
    });

    const orchestrator = new Orchestrator(workflow(), logger);
    const o = orchestrator as unknown as {
      reviewerQuotaCooldownUntil: number;
      dispatchTick(): Promise<void>;
    };
    o.reviewerQuotaCooldownUntil = Date.now() + 120_000;

    await o.dispatchTick();

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(pressureSpy).not.toHaveBeenCalled();
    expect(orchestrator.snapshot().reviewer_quota_cooldown_until_ms).toBeGreaterThan(Date.now());

    fetchSpy.mockRestore();
    pressureSpy.mockRestore();
  });
});
