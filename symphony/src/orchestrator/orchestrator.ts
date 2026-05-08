import { spawn } from "node:child_process";
import { LinearClient } from "../tracker/linear.js";
import { WorkspaceManager } from "../workspace.js";
import { getRunner, runnerNameFor, getFailoverState } from "../agent/registry.js";
import { renderPrompt } from "../workflow-loader.js";
import type { Workflow, Issue, RunningSession, RetryEntry } from "../types.js";
import type { Logger } from "../logger.js";
import { createTelegramNotifier, type SymphonyNotifier } from "../notify/telegram.js";
import { readProcessPressure, type ProcessPressure } from "../process-pressure.js";
import { isReviewerQuotaOrRateLimit, reviewerQuotaCooldownMs } from "../reviewer-quota.js";

const DONE_MARKER = /\[symphony:done\]/i;

/** Detect LangGraph "empty diff" failures that should accrue toward a park-after-N streak. */
export function isLanggraphWorkerNoChangesFailure(reason: string): boolean {
  const s = reason.toLowerCase();
  return (
    /\bworker_no_changes\b/.test(s) ||
    s.includes("no new commit") ||
    s.includes("head unchanged") ||
    s.includes("empty diff")
  );
}

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
  private failureAttempts = new Map<string, number>();
  /** Consecutive LangGraph turns with worker_no_changes / empty diff, per Linear issue id. */
  private noChangesStreak = new Map<string, number>();
  private completed = new Set<string>();
  private totals = { input: 0, output: 0, total: 0, runtime_seconds: 0 };
  private pressure: ProcessPressure | null = null;
  private linear: LinearClient;
  private ws: WorkspaceManager;
  private notifier: SymphonyNotifier;
  private pollTimer: NodeJS.Timeout | null = null;
  private stopped = false;
  private log: Logger;
  /** When set, skip new dispatches (and defer retries) until this epoch ms — e.g. LLM reviewer 429/quota. */
  private reviewerQuotaCooldownUntil = 0;
  private lastReviewerCooldownLogAt = 0;

  constructor(private workflow: Workflow, log: Logger) {
    this.linear = new LinearClient(workflow.config.tracker);
    this.ws = new WorkspaceManager(workflow.config, log);
    this.notifier = createTelegramNotifier(log);
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
    // Runner is resolved at snapshot-time, same way it's resolved at dispatch-
    // time, so the dashboard sees whatever the next dispatch will use. When
    // Phase 2's failover detector lands, the snapshot will reflect the
    // currently-active tier — same call.
    const runner = runnerNameFor(this.workflow.config);
    const model = this.workflow.config.agent.model ?? null;
    const failover = getFailoverState();
    return {
      runner,
      model,
      posture: this.workflow.config.posture,
      failover,
      process_pressure: this.pressure,
      reviewer_quota_cooldown_until_ms:
        this.reviewerQuotaCooldownUntil > Date.now() ? this.reviewerQuotaCooldownUntil : null,
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
    
    const failover = getFailoverState();
    const runnerName = runnerNameFor(this.workflow.config);

    let stallMs = this.workflow.config.codex.stall_timeout_ms;
    if (failover.inFailover || runnerName === "aider") {
      stallMs = this.workflow.config.aider?.stall_timeout_ms ?? 600_000;
    } else if (runnerName === "langgraph") {
      stallMs = this.workflow.config.langgraph?.stall_timeout_ms ?? 600_000;
    }
    
    const now = Date.now();
    for (const [id, s] of this.running) {
      // Sessions update s.lastEventAt on every stdout line (including heartbeats).
      // If we hit stallMs, it means the process has been completely silent.
      if (stallMs > 0 && now - s.lastEventAt > stallMs && s.phase === "LaunchingAgentProcess") {
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
    if (Date.now() < this.reviewerQuotaCooldownUntil) {
      const remaining = this.reviewerQuotaCooldownUntil - Date.now();
      if (Date.now() - this.lastReviewerCooldownLogAt > 60_000) {
        this.lastReviewerCooldownLogAt = Date.now();
        this.log.warn("dispatch.deferred_reviewer_quota", { remaining_ms: remaining });
      }
      return;
    }
    this.pressure = await readProcessPressure();
    if (!this.pressure.ok) {
      this.log.warn("dispatch.deferred_process_pressure", {
        reason: this.pressure.reason,
        load1: this.pressure.load1,
        free_mem_mb: Math.round(this.pressure.freeMemMb),
        risky_process_count: this.pressure.riskyProcessCount,
        risky_process_sample: this.pressure.riskyProcessSample,
      });
      return;
    }

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
    this.failureAttempts.delete(issueId);
    this.noChangesStreak.delete(issueId);
    const r = this.retries.get(issueId);
    if (r) {
      clearTimeout(r.timer);
      this.retries.delete(issueId);
    }
  }

  private async runWorker(issue: Issue): Promise<void> {
    const log = this.log.child({ issue_id: issue.id, issue_identifier: issue.identifier });
    if (Date.now() < this.reviewerQuotaCooldownUntil) {
      const remaining = this.reviewerQuotaCooldownUntil - Date.now();
      log.warn("worker.skip_dispatch_reviewer_quota", {
        remaining_ms: remaining,
      });
      this.claimed.delete(issue.id);
      return;
    }
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

    // Transition Linear state to In Progress on first dispatch (best-effort, non-fatal).
    // markIssueStarted no-ops if the issue is already in a started-type state, so
    // continuation turns and resumed sessions don't generate redundant Linear writes
    // — and don't generate redundant Telegram pings either: we only fire `started`
    // when the transition actually moved the issue (reason !== "already started").
    void this.linear
      .markIssueStarted(issue.id)
      .then((r) => {
        if (!r.ok) {
          log.warn("linear.mark_started_failed", { reason: r.reason });
          return;
        }
        if (r.reason !== "already started") {
          this.notifier.started(issue);
        }
      })
      .catch((err) => log.warn("linear.mark_started_error", { err: String(err) }));

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

      let usedFallback = false;

      const existingMsg = await lastCommitMessage(created.path);
      if (DONE_MARKER.test(existingMsg)) {
        log.info("worker.preexisting_completion_marker_found", { commit_msg: existingMsg.slice(0, 200) });
        const r = await this.linear.markIssueDone(issue.id).catch((err) => ({
          ok: false as const,
          reason: String(err),
        }));
        if (r.ok) log.info("worker.linear_marked_done");
        else log.warn("worker.linear_mark_failed", { reason: r.reason });
        this.notifier.done(issue, {
          turns: session.turnCount,
          tokens: session.totals.total,
          usedFallback,
        });
        session.phase = "Succeeded";
        this.completed.add(issue.id);
        this.release(issue.id);
        return;
      }

      const lg = this.workflow.config.langgraph;
      if (lg?.pre_dispatch_shell?.trim()) {
        const pre = await this.runPreDispatchShell(created.path, issue);
        if (pre.code === 77) {
          log.info("worker.pre_dispatch_skip", { stderr: pre.stderr.slice(0, 400) });
          session.phase = "Failed";
          await this.parkIssueForHuman(
            issue,
            "Pre-dispatch hook exited **77** (skip) — see stderr below.",
            pre.stderr,
            log,
          );
          this.release(issue.id);
          return;
        }
        if (pre.code !== 0) {
          throw new Error(`pre_dispatch_failed: exit ${pre.code}: ${pre.stderr.slice(0, 500)}`);
        }
      }

      while (session.turnCount < this.workflow.config.agent.max_turns) {
        if (abort.signal.aborted) break;
        await this.ws.runBeforeRun(issue.identifier);
        session.phase = "BuildingPrompt";
        const prompt = await renderPrompt(
          this.workflow.promptTemplate,
          issue,
          priorAttempt ?? (session.turnCount > 0 ? session.turnCount + 1 : null),
          this.workflow.config.posture,
        );
        session.phase = "LaunchingAgentProcess";
        // Resolve the runner per turn (not per orchestrator) so Phase 2's
        // failover detector can swap runners mid-flight when a quota
        // threshold trips, without needing to restart the orchestrator.
        const { name: runnerName, runner } = getRunner(this.workflow.config);
        log.debug("agent.runner_selected", { runner: runnerName });
        const turn = await runner({
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
        if (turn.usedFallback) usedFallback = true;
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
          this.armReviewerQuotaCooldownIfNeeded(turn.reason ?? "");
          throw new Error(turn.reason ?? "turn_failed");
        }
        this.noChangesStreak.delete(issue.id);

        // Completion marker: if the agent's last commit message contains [symphony:done],
        // mark the Linear issue Done so the harness doesn't keep dispatching turns.
        const msg = await lastCommitMessage(created.path);
        if (DONE_MARKER.test(msg)) {
          log.info("worker.completion_marker_found", { commit_msg: msg.slice(0, 200) });
          
          if (usedFallback) {
            log.info("worker.posting_fallback_comment");
            
            // Extract the GPT-5.5 feedback from the events array
            const feedbackEvents = turn.events.filter(e => e.kind === "assistant_text" && (e as any).text.startsWith("GPT-5.5 Feedback:"));
            const allFeedback = feedbackEvents.map(e => (e as any).text).join("\n\n---\n\n");
            
            const commentBody = allFeedback 
              ? `**Senior Engineer (GPT-5.5) Review Loops:**\n\n${allFeedback}`
              : `**Senior Engineer (GPT-5.5) Review:**\n\nAPPROVED on the first try.`;

            await this.linear.addCommentToIssue(issue.id, commentBody)
              .catch((err) => log.warn("linear.comment_failed", { err: String(err) }));
          }

          const r = await this.linear.markIssueDone(issue.id).catch((err) => ({
            ok: false as const,
            reason: String(err),
          }));
          if (r.ok) log.info("worker.linear_marked_done");
          else log.warn("worker.linear_mark_failed", { reason: r.reason });
          this.notifier.done(issue, {
            turns: session.turnCount,
            tokens: session.totals.total,
            usedFallback,
          });
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
      const msg = err instanceof Error ? err.message : String(err);
      if (session.phase === "CanceledByReconciliation") {
        log.info("worker.canceled_by_reconciliation", { reason: msg });
        this.release(issue.id);
        return;
      }
      session.phase = "Failed";

      const isLg = runnerNameFor(this.workflow.config) === "langgraph";
      if (isLg && isLanggraphWorkerNoChangesFailure(msg)) {
        const max = this.workflow.config.langgraph?.max_consecutive_worker_no_changes ?? 3;
        const next = (this.noChangesStreak.get(issue.id) ?? 0) + 1;
        this.noChangesStreak.set(issue.id, next);
        if (next >= max) {
          this.noChangesStreak.delete(issue.id);
          log.warn("worker.park_empty_diff_loop", { identifier: issue.identifier, streak: next });
          this.armReviewerQuotaCooldownIfNeeded(msg);
          await this.parkIssueForHuman(
            issue,
            `LangGraph **worker_no_changes** (${next}×) — empty git diff each run. Likely already on main or scope needs tightening.`,
            msg,
            log,
          );
          this.running.delete(issue.id);
          this.release(issue.id);
          return;
        }
      } else if (isLg) {
        this.noChangesStreak.delete(issue.id);
      }

      this.armReviewerQuotaCooldownIfNeeded(msg);
      log.error("worker.failed", { err: msg, turns: session.turnCount });
      this.running.delete(issue.id);
      this.scheduleRetry(issue, msg, undefined, false);
    }
  }

  private async parkIssueForHuman(issue: Issue, headline: string, detail: string, log: Logger): Promise<void> {
    const detailBlock =
      detail.trim().length > 0 ? `\n\n\`\`\`\n${detail.trim().slice(0, 2000)}\n\`\`\`` : "";
    const body = [
      "**Symphony automation**",
      "",
      headline,
      "",
      "<!-- symphony-park:v1 -->",
      "",
      "Moved issue to **In Review** to stop an autonomous retry/token loop. Move back to **Todo** or **In Progress** when you want another run.",
      detailBlock,
    ].join("\n");
    await this.linear.addCommentToIssue(issue.id, body).catch((err) => log.warn("linear.comment_failed", { err: String(err) }));
    const r = await this.linear.markIssueInReview(issue.id).catch((err) => ({
      ok: false as const,
      reason: String(err),
    }));
    if (r.ok) log.info("worker.linear_marked_in_review");
    else log.warn("worker.linear_mark_in_review_failed", { reason: "reason" in r ? r.reason : String(r) });
  }

  private runPreDispatchShell(cwd: string, issue: Issue): Promise<{ code: number; stderr: string }> {
    const script = this.workflow.config.langgraph?.pre_dispatch_shell?.trim();
    if (!script) return Promise.resolve({ code: 0, stderr: "" });
    return new Promise((resolve) => {
      const child = spawn("bash", ["-lc", script], {
        cwd,
        env: {
          ...process.env,
          SYMPHONY_ISSUE_IDENTIFIER: issue.identifier,
          SYMPHONY_ISSUE_ID: issue.id,
          SYMPHONY_ISSUE_TITLE: issue.title ?? "",
          SYMPHONY_REPO_ROOT: cwd,
        },
        stdio: ["ignore", "ignore", "pipe"],
      });
      let stderr = "";
      child.stderr?.setEncoding("utf8");
      child.stderr?.on("data", (chunk: string) => {
        stderr += chunk;
        if (stderr.length > 8000) stderr = stderr.slice(-8000);
      });
      child.once("close", (code) => resolve({ code: code ?? 1, stderr }));
      child.once("error", (err) => resolve({ code: 1, stderr: String(err) }));
    });
  }

  private armReviewerQuotaCooldownIfNeeded(reason: string): void {
    if (!isReviewerQuotaOrRateLimit(reason)) return;
    const ms = reviewerQuotaCooldownMs();
    const until = Date.now() + ms;
    this.reviewerQuotaCooldownUntil = Math.max(this.reviewerQuotaCooldownUntil, until);
    this.log.warn("reviewer.quota_cooldown_armed", {
      cooldown_ms: ms,
      until_epoch_ms: this.reviewerQuotaCooldownUntil,
    });
  }

  private scheduleRetry(
    issue: Issue,
    reason: string,
    fixedDelayMs?: number,
    postSuccess = false,
    holdAttempt = false,
  ): void {
    if (this.stopped) return;
    const prior = this.retries.get(issue.id);
    let attempt: number;
    if (postSuccess) {
      attempt = 0;
      this.failureAttempts.delete(issue.id);
    } else if (holdAttempt) {
      attempt = this.failureAttempts.get(issue.id) ?? 0;
    } else {
      attempt = (this.failureAttempts.get(issue.id) ?? 0) + 1;
      this.failureAttempts.set(issue.id, attempt);
    }
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
    // Telegram-ping only on first failure. Backoff already escalates fast and
    // subsequent retries are visible on the :4754 dashboard — pinging every
    // retry would drown the chat for any flaky issue.
    if (!postSuccess && !holdAttempt && attempt === 1) {
      this.notifier.failed(issue, { attempt, lastError: reason });
    }
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
    if (Date.now() < this.reviewerQuotaCooldownUntil) {
      const wait = Math.max(this.reviewerQuotaCooldownUntil - Date.now(), 5000);
      this.scheduleRetry(found, "reviewer_quota_cooldown", wait, false, true);
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