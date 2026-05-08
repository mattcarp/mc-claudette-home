import { execFile } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunOptions } from "./runner.js";
import type { WorkflowConfig, RunningSession } from "../types.js";
import type { Logger } from "../logger.js";
import { runFallbackPipeline } from "./fallback-runner.js";
import { runAiderTurn } from "./aider-runner.js";

vi.mock("./aider-runner.js", () => ({
  runAiderTurn: vi.fn(),
}));

const execFileAsync = promisify(execFile);
const mockedRunAiderTurn = vi.mocked(runAiderTurn);

const logger: Logger = {
  info: vi.fn(),
  warn: vi.fn(),
  error: vi.fn(),
  debug: vi.fn(),
  child: () => logger,
};

function config(): WorkflowConfig {
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
      runner: "claude-code",
      fallback: [],
    },
    codex: {
      command: "claude -p",
      turn_timeout_ms: 60_000,
      read_timeout_ms: 1_000,
      stall_timeout_ms: 30_000,
    },
    aider: {
      command: "aider",
      turn_timeout_ms: 60_000,
      stall_timeout_ms: 30_000,
      show_resource_usage: true,
    },
    server: { host: "127.0.0.1", port: 4754 },
  };
}

function session(): RunningSession {
  return {
    issueId: "issue-id",
    identifier: "MAG-32",
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

async function makeRepo(): Promise<string> {
  const cwd = await mkdtemp(join(tmpdir(), "symphony-fallback-test-"));
  await execFileAsync("git", ["init"], { cwd });
  await execFileAsync("git", ["config", "user.email", "test@example.com"], { cwd });
  await execFileAsync("git", ["config", "user.name", "Test Runner"], { cwd });
  await writeFile(join(cwd, "README.md"), "test\n", "utf8");
  await execFileAsync("git", ["add", "README.md"], { cwd });
  await execFileAsync("git", ["commit", "-m", "initial"], { cwd });
  return cwd;
}

async function lastCommit(cwd: string): Promise<string> {
  const { stdout } = await execFileAsync("git", ["log", "-1", "--format=%B"], { cwd });
  return stdout;
}

function options(cwd: string, activeSession: RunningSession): RunOptions {
  return {
    cwd,
    prompt: "Do the MAG-32 task.",
    resumeSessionId: null,
    log: logger,
    abort: activeSession.abort.signal,
    session: activeSession,
    cfg: config(),
  };
}

describe("runFallbackPipeline", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ data: { commentCreate: { success: true } } }),
      })),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps child Aider activity on the parent running session and finalizes approved work", async () => {
    const cwd = await makeRepo();
    const activeSession = session();

    mockedRunAiderTurn
      .mockImplementationOnce(async (opts) => {
        opts.session.sessionId = "generator-session";
        opts.session.lastEventAt = Date.now() + 1_000;
        return { ok: true, sessionId: "generator-session", usage: {}, events: [] };
      })
      .mockImplementationOnce(async (opts) => {
        opts.session.sessionId = "critic-session";
        opts.session.lastEventAt = Date.now() + 2_000;
        await writeFile(join(opts.cwd, "symphony-review.md"), "APPROVED", "utf8");
        return { ok: true, sessionId: "critic-session", usage: {}, events: [] };
      });

    const result = await runFallbackPipeline(options(cwd, activeSession));

    expect(result.ok).toBe(true);
    expect(activeSession.sessionId).toBe("critic-session");
    expect(activeSession.lastEventAt).toBeGreaterThan(activeSession.startedAt);
    await expect(lastCommit(cwd)).resolves.toMatch(/\[symphony:done\]/i);
  });

  it("fails closed when the reviewer never approves instead of reporting a successful turn", async () => {
    const cwd = await makeRepo();
    const activeSession = session();

    mockedRunAiderTurn.mockImplementation(async (opts) => {
      if (opts.cfg.agent.model === "openai/gpt-5.5") {
        await writeFile(join(opts.cwd, "symphony-review.md"), "Needs more work.", "utf8");
      }
      opts.session.lastEventAt = Date.now() + 1_000;
      return { ok: true, sessionId: "aider-session", usage: {}, events: [] };
    });

    const result = await runFallbackPipeline(options(cwd, activeSession));

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("fallback_review_not_approved_after_3_rounds");
  });

  it("treats missing reviewer output as a hard failure without posting to Linear", async () => {
    const cwd = await makeRepo();
    const activeSession = session();

    mockedRunAiderTurn.mockImplementation(async (opts) => {
      opts.session.lastEventAt = Date.now() + 1_000;
      // GPT-5.5 returns ok but writes no file and emits no assistant_text events
      return { ok: true, sessionId: "aider-session", usage: {}, events: [] };
    });

    const fetchMock = vi.mocked(fetch);
    const result = await runFallbackPipeline(options(cwd, activeSession));

    expect(result.ok).toBe(false);
    expect(result.reason).toBe("fallback_reviewer_no_output");
    // Linear must not have been called with a misleading "no feedback" comment
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts approval comment only after the done commit is created", async () => {
    const cwd = await makeRepo();
    const activeSession = session();

    const callOrder: string[] = [];

    mockedRunAiderTurn
      .mockImplementationOnce(async (opts) => {
        opts.session.lastEventAt = Date.now() + 1_000;
        return { ok: true, sessionId: "generator-session", usage: {}, events: [] };
      })
      .mockImplementationOnce(async (opts) => {
        opts.session.lastEventAt = Date.now() + 2_000;
        await writeFile(join(opts.cwd, "symphony-review.md"), "APPROVED", "utf8");
        return { ok: true, sessionId: "critic-session", usage: {}, events: [] };
      });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        callOrder.push("linear");
        return { ok: true, json: async () => ({ data: { commentCreate: { success: true } } }) };
      }),
    );

    const result = await runFallbackPipeline(options(cwd, activeSession));

    expect(result.ok).toBe(true);
    const commit = await lastCommit(cwd);
    expect(commit).toMatch(/\[symphony:done\]/i);
    // fetch (Linear comment) must be called only once and only after commit exists
    expect(callOrder).toEqual(["linear"]);
  });

  it("does not overwrite newer child activity timestamps when starting subturns", async () => {
    const cwd = await makeRepo();
    const activeSession = session();
    const newerActivity = Date.now() + 60_000;
    activeSession.lastEventAt = newerActivity;

    mockedRunAiderTurn
      .mockImplementationOnce(async (opts) => {
        expect(opts.session.lastEventAt).toBe(newerActivity);
        return { ok: true, sessionId: "generator-session", usage: {}, events: [] };
      })
      .mockImplementationOnce(async (opts) => {
        expect(opts.session.lastEventAt).toBe(newerActivity);
        await writeFile(join(opts.cwd, "symphony-review.md"), "APPROVED", "utf8");
        return { ok: true, sessionId: "critic-session", usage: {}, events: [] };
      });

    const result = await runFallbackPipeline(options(cwd, activeSession));

    expect(result.ok).toBe(true);
    expect(activeSession.lastEventAt).toBe(newerActivity);
  });
});
