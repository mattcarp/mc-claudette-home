import { spawn } from "node:child_process";
import { LinearClient } from "../tracker/linear.js";
import { WorkspaceManager } from "../workspace.js";
import { runAgentTurn } from "../agent/runner.js";
import { renderPrompt } from "../workflow-loader.js";
import type { Workflow, Issue, RunningSession, RetryEntry } from "../types.js";
import type { Logger } from "../logger.js";

const DONE_MARKER = /\[symphony:done\]/i;

function lastCommitMessage(cwd: string): Promise<string> {
  return new Promise((resolveCmd) => {
    const child = spawn("git", ["log", "-1", "--format=%B"], { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (b) => (out += b.toString()));
    child.once("close", () => resolveCmd(out.trim()));
    child.once("error", () => resolveCmd(""));
  });
}

interface DispatchSlot {
  identifier: string;
  issueId: string;
}

export class Orchestrator {
  private running = new Map<string, RunningSession>(); // issueId -> session
  private claimed = new Set<string>();
  private retries = new Map<string, RetryEntry>();
  private completed = new Set<string>();
  private totals = { input: 0, output: 0, total: 0, runtime_seconds: 0 };
  private linear: LinearClient;
  private ws: WorkspaceManager;
  private pollTimer: NodeJS.Timeout | null = null;
  private stopped = false;
  private log: Logger;

  constructor(private workflow: Workflow, log: Logger) {
    this.linear = new LinearClient(workflow.config.tracker);
    this.ws = new WorkspaceManager(workflow.config, log);
    this.log = log;
  }

  async start(): Promise<void> {
    await this.ws.ensureRoot();
    await this.startupCleanup().catch((err) =>
      this.log.warn("startup cleanup failed", { err: String(err) }),
    );
    this.log.info("orchestrator.start", {
      workspace_root: this.ws.rootPath(),
      poll_ms: this.workflow.config.polling.interval_ms,
      max_concurrent: this.workflow.config.agent.max_concurrent_agents,
    });
    this.scheduleTick(0);
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.pollTimer) clearTimeout(this.pollTimer);
    for (const r of this.retries.values()) clearTimeout(r.timer);
    this.retries.clear();
    for (const s of this.running.values()) s.abort.abort();
    // Wait briefly for sessions to die
    await new Promise((r) => setTimeout(r, 1000));
  }

  snapshot() {
    return {
      running: Array.from(this.running.values()).map((s) => ({
        identifier: s.identifier,
        issue_id: s.issueId,
        phase: s.phase,
        turns: s.turnCount,
        session_id: s.sessionId,
        last_event_age_ms: Date.now() - s.lastEventAt,
        totals: s.totals,
      })),
      retrying: Array.from(this.retries.values()).map((r) => ({
        identifier: r.identifier,
        attempt: r.attempt,
        due_in_ms: Math.max(r.dueAt - Date.now(), 0),
        last_error: r.lastError,
      })),
      totals: this.totals,
    };
  }

  private scheduleTick(delayMs: number): void {
    if (this.stopped) return;
    this.pollTimer = setTimeout(() => {
      void this.tick();
    }, delayMs);
    this.pollTimer.unref();
  }

  private async tick(): Promise<void> {
    if (this.stopped) return;
    try {
      await this.reconcileRunning();
      await this.dispatchTick();
    } catch (err) {
      this.log.error("tick failed", { err: String(err) });
    } finally {
      this.scheduleTick(this.workflow.config.polling.interval_ms);
    }
  }

  private async reconcileRunning(): Promise<void> {
    if (this.running.size === 0) return;
    const stallMs = this.workflow.config.codex.stall_timeout_ms;
    const now = Date.now();
    for (const [id, s] of this.running) {
      if (stallMs > 0 && now - s.lastEventAt > stallMs && s.phase === "StreamingTurn") {
        this.log.warn("reconcile.stall_detected", {
          identifier: s.identifier,
          since_last_event_ms: now - s.lastEventAt,
        });
        s.abort.abort();
      }
    }
    const ids = Array.from(this.running.keys());
    let states: Map<string, string>;
    try {
      states = await this.linear.fetchStatesByIds(ids);
    } catch (err) {
      this.log.warn("reconcile.state_refresh_failed", { err: String(err) });
      return;
    }
    const terminal = new Set(this.workflow.config.tracker.terminal_states.map((s) => s.toLowerCase()));
    for (const id of ids) {
      const s = this.running.get(id);
      if (!s) continue;
      const state = states.get(id);
      if (!state) {
        this.log.info("reconcile.issue_missing", { identifier: s.identifier });
        s.abort.abort();
        continue;
      }
      if (terminal.has(state.toLowerCase())) {
        this.log.info("reconcile.issue_terminal", { identifier: s.identifier, state });
        s.phase = "CanceledByReconciliation";
        s.abort.abort();
      }
    }
  }

  private async dispatchTick(): Promise<void> {
    const cfg = this.workflow.config;
    let candidates: Issue[];
    try {
      candidates = await this.linear.fetchCandidateIssues();
    } catch (err) {
      this.log.warn("dispatch.fetch_failed", { err: String(err) });
      return;
    }
    candidates.sort((a, b) => {
      const pa = a.priority ?? 99;
      const pb = b.priority ?? 99;
      if (pa !== pb) return pa - pb;
      const ta = Date.parse(a.created_at);
      const tb = Date.parse(b.created_at);
      if (ta !== tb) return ta - tb;
      return a.identifier.localeCompare(b.identifier);
    });

    const slotsAvail = () =>
      Math.max(cfg.agent.max_concurrent_agents - this.running.size, 0);

    for (const issue of candidates) {
      if (slotsAvail() <= 0) break;
      if (!this.eligible(issue)) continue;
      this.claim(issue);
      void this.runWorker(issue);
    }
  }

  private eligible(issue: Issue): boolean {
    const cfg = this.workflow.config;
    const active = new Set(cfg.tracker.active_states.map((s) => s.toLowerCase()));
    const terminal = new Set(cfg.tracker.terminal_states.map((s) => s.toLowerCase()));
    const state = issue.state.toLowerCase();
    if (!active.has(state) || terminal.has(state)) return false;
    if (this.running.has(issue.id) || this.claimed.has(issue.id)) return false;
    if (state === "todo" && issue.blocked_by.length > 0) return false;
    return true;
  }

  private claim(issue: Issue): void {
    this.claimed.add(issue.id);
  }

  private release(issueId: string): void {
    this.claimed.delete(issueId);
    this.running.delete(issueId);
    const r = this.retries.get(issueId);
    if (r) {
      clearTimeout(r.timer);
      this.retries.delete(issueId);
    }
  }

  private async runWorker(issue: Issue): Promise<void> {
    const log = this.log.child({ issue_id: issue.id, issue_identifier: issue.identifier });
    const abort = new AbortController();
    const session: RunningSession = {
      issueId: issue.id,
      identifier: issue.identifier,
      workspacePath: "",
      startedAt: Date.now(),
      lastEventAt: Date.now(),
      turnCount: 0,
      phase: "PreparingWorkspace",
      sessionId: null,
      abort,
      totals: { input: 0, output: 0, total: 0 },
    };
    this.running.set(issue.id, session);

    let priorAttempt = this.retries.get(issue.id)?.attempt ?? null;
    if (priorAttempt !== null) {
      clearTimeout(this.retries.get(issue.id)!.timer);
      this.retries.delete(issue.id);
    }

    try {
      const created = await this.ws.create(issue.identifier);
      session.workspacePath = created.path;
      log.info("worker.workspace_ready", {
        path: created.path,
        created_now: created.createdNow,
      });

      while (session.turnCount < this.workflow.config.agent.max_turns) {
        if (abort.signal.aborted) break;
        await this.ws.runBeforeRun(issue.identifier);
        session.phase = "BuildingPrompt";
        const prompt = await renderPrompt(
          this.workflow.promptTemplate,
          issue,
          priorAttempt ?? (session.turnCount > 0 ? session.turnCount + 1 : null),
        );
        session.phase = "LaunchingAgentProcess";
        const turn = await runAgentTurn({
          cwd: created.path,
          prompt:
            session.turnCount === 0
              ? prompt
              : `Continue working on ${issue.identifier}. The issue is still active. Take the next sensible step.`,
          resumeSessionId: session.sessionId,
          log,
          abort: abort.signal,
          session,
          cfg: this.workflow.config,
        });
        session.turnCount += 1;
        if (turn.usage) {
          const u = turn.usage;
          session.totals.input += u.input_tokens ?? 0;
          session.totals.output += u.output_tokens ?? 0;
          session.totals.total += u.total_tokens ?? 0;
          this.totals.input += u.input_tokens ?? 0;
          this.totals.output += u.output_tokens ?? 0;
          this.totals.total += u.total_tokens ?? 0;
        }
        await this.ws.runAfterRun(issue.identifier);
        if (!turn.ok) {
          throw new Error(turn.reason ?? "turn_failed");
        }

        // Completion marker: if the agent's last commit message contains [symphony:done],
        // mark the Linear issue Done so the harness doesn't keep dispatching turns.
        const msg = await lastCommitMessage(created.path);
        if (DONE_MARKER.test(msg)) {
          log.info("worker.completion_marker_found", { commit_msg: msg.slice(0, 200) });
          const r = await this.linear.markIssueDone(issue.id).catch((err) => ({
            ok: false as const,
            reason: String(err),
          }));
          if (r.ok) log.info("worker.linear_marked_done");
          else log.warn("worker.linear_mark_failed", { reason: r.reason });
          break;
        }

        // After a successful turn, refresh state. If still active, continue; else break.
        const states = await this.linear.fetchStatesByIds([issue.id]).catch(() => new Map<string, string>());
        const newState = states.get(issue.id);
        const terminal = new Set(
          this.workflow.config.tracker.terminal_states.map((s) => s.toLowerCase()),
        );
        const active = new Set(
          this.workflow.config.tracker.active_states.map((s) => s.toLowerCase()),
        );
        if (!newState || terminal.has(newState.toLowerCase()) || !active.has(newState.toLowerCase())) {
          break;
        }
      }

      session.phase = "Succeeded";
      log.info("worker.completed", {
        turns: session.turnCount,
        totals: session.totals,
        session_id: session.sessionId,
      });
      this.completed.add(issue.id);
      this.release(issue.id);
      // Continuation retry: short delay re-check, then potentially redispatch.
      this.scheduleRetry(issue, "post_completion_recheck", 1000, true);
    } catch (err) {
      session.phase = "Failed";
      const msg = err instanceof Error ? err.message : String(err);
      log.error("worker.failed", { err: msg, turns: session.turnCount });
      this.running.delete(issue.id);
      this.scheduleRetry(issue, msg, undefined, false);
    }
  }

  private scheduleRetry(issue: Issue, reason: string, fixedDelayMs?: number, postSuccess = false): void {
    if (this.stopped) return;
    const prior = this.retries.get(issue.id);
    const attempt = postSuccess ? 0 : (prior?.attempt ?? 0) + 1;
    const cfg = this.workflow.config.agent;
    const computed = fixedDelayMs ?? Math.min(10_000 * 2 ** (attempt - 1), cfg.max_retry_backoff_ms);
    const dueAt = Date.now() + computed;
    if (prior) clearTimeout(prior.timer);
    const timer = setTimeout(() => {
      void this.handleRetryFire(issue.id);
    }, computed);
    timer.unref();
    this.retries.set(issue.id, {
      issueId: issue.id,
      identifier: issue.identifier,
      attempt,
      dueAt,
      lastError: reason,
      timer,
    });
    this.log.info("retry.scheduled", {
      identifier: issue.identifier,
      attempt,
      due_in_ms: computed,
      reason,
    });
  }

  private async handleRetryFire(issueId: string): Promise<void> {
    const r = this.retries.get(issueId);
    if (!r) return;
    this.retries.delete(issueId);
    let candidates: Issue[];
    try {
      candidates = await this.linear.fetchCandidateIssues();
    } catch (err) {
      this.log.warn("retry.fetch_failed", { identifier: r.identifier, err: String(err) });
      // Reschedule with the same backoff curve
      this.scheduleRetry(
        { id: r.issueId, identifier: r.identifier } as Issue,
        `retry_fetch_failed: ${String(err)}`,
        undefined,
        false,
      );
      return;
    }
    const found = candidates.find((c) => c.id === r.issueId);
    if (!found) {
      this.log.info("retry.released", { identifier: r.identifier, reason: "not_in_candidates" });
      this.release(issueId);
      return;
    }
    const slots = Math.max(
      this.workflow.config.agent.max_concurrent_agents - this.running.size,
      0,
    );
    if (slots <= 0) {
      this.scheduleRetry(found, "no_slots", undefined, false);
      return;
    }
    this.claim(found);
    void this.runWorker(found);
  }

  private async startupCleanup(): Promise<void> {
    const terminalStates = this.workflow.config.tracker.terminal_states;
    const issues = await this.linear.fetchByStates(terminalStates);
    for (const issue of issues) {
      try {
        await this.ws.remove(issue.identifier);
      } catch (err) {
        this.log.debug("startup_cleanup.remove_failed", {
          identifier: issue.identifier,
          err: String(err),
        });
      }
    }
  }
}
