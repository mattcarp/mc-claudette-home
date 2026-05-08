// Claude Code runner — spawns `claude -p --output-format=stream-json` and
// parses the resulting JSONL event stream. Original implementation, lifted
// out of runner.ts so the runner-shape abstraction can sit cleanly above it.
//
// Reads its config from `cfg.codex.*` (kept as the historical block name so
// existing WORKFLOW.md files don't need migration).
//
// Stream-json events handled:
//   system/init         -> session_started
//   assistant/user msg  -> tool_use, assistant_text
//   result              -> turn_completed (with usage)
//   system/hook_*       -> dropped (firehose noise)
//   anything else       -> other_message
//
// Behaviors enforced regardless of model:
//   - prompt sent on stdin, stdin closed (single-shot)
//   - stall_timeout_ms: SIGTERM if no event for this long
//   - turn_timeout_ms:  SIGTERM unconditional ceiling
//   - abort signal:     SIGTERM on orchestrator cancellation
//   - env scrub:        drop ANTHROPIC_*, CLAUDE_API_*, CLAUDE_CODE_API_*
//                       so OAuth token in ~/.claude/.credentials.json wins
//                       over any stale upstream API key.

import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { createInterface } from "node:readline";
import type { AgentEvent, AgentRunResult, RunOptions, Usage } from "./runner.js";
import { enforceProductionPosture } from "./posture.js";

function shellQuote(s: string): string {
  if (/^[A-Za-z0-9_./:-]+$/.test(s)) return s;
  return `'${s.replace(/'/g, "'\\''")}'`;
}

/**
 * Runs one Claude Code turn in headless stream-json mode.
 */
export async function runClaudeCodeTurn(opts: RunOptions): Promise<AgentRunResult> {
  const { cwd, prompt, resumeSessionId, log, abort, session, cfg } = opts;
  const baseCmd = cfg.codex.command.trim();
  if (!baseCmd) {
    return {
      ok: false,
      reason: "codex_not_found: empty command",
      sessionId: resumeSessionId,
      usage: {},
      events: [],
    };
  }
  const fullCmd = resumeSessionId
    ? `${baseCmd} --resume ${shellQuote(resumeSessionId)}`
    : baseCmd;

  let finalCmd = fullCmd;
  if (cfg.posture === "production") {
    finalCmd = enforceProductionPosture(finalCmd, log);
  }

  const cleanEnv: NodeJS.ProcessEnv = {
    // Prevent Claude CLI from trying to open the browser or desktop apps 
    // for OAuth/auth flows when running headlessly in the background.
    BROWSER: "none",
  };
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined) continue;
    if (/^(ANTHROPIC_|CLAUDE_API|CLAUDE_CODE_API)/i.test(k)) continue;
    // Don't overwrite the BROWSER variable we just set
    if (k === "BROWSER") continue;
    cleanEnv[k] = v;
  }

  log.info("agent.spawn", {
    runner: "claude-code",
    cmd: fullCmd.replace(/\s+/g, " ").slice(0, 200),
    cwd,
  });

  let child: ChildProcessByStdio<Writable, Readable, Readable>;
  try {
      child = spawn("bash", ["-lc", finalCmd], {
      cwd,
      env: cleanEnv,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (err) {
    return {
      ok: false,
      reason: `codex_not_found: ${String(err)}`,
      sessionId: resumeSessionId,
      usage: {},
      events: [],
    };
  }

  const events: AgentEvent[] = [];
  let sessionId: string | null = resumeSessionId;
  let usage: Usage = {};
  let resultMessage: string | undefined;
  let resultIsError = false;
  let exitReason: string | null = null;

  child.stdin.write(prompt);
  child.stdin.end();

  const stallMs = cfg.codex.stall_timeout_ms;
  const turnMs = cfg.codex.turn_timeout_ms;

  let stallTimer: NodeJS.Timeout | undefined;
  const armStall = () => {
    if (stallMs <= 0) return;
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      exitReason = exitReason ?? "stalled";
      events.push({ kind: "stalled", elapsedMs: Date.now() - session.lastEventAt });
      log.warn("agent.stalled", {
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
    log.warn("agent.turn_timeout", { turnMs });
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
  child.stderr.on("data", (b) => {
    const s = b.toString();
    if (stderrChunks.length < 50) stderrChunks.push(s);
  });

  const rl = createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    session.lastEventAt = Date.now();
    armStall();
    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      events.push({ kind: "malformed", line: line.slice(0, 500) });
      return;
    }
    handleStreamEvent(obj, {
      onSession: (id) => {
        sessionId = id;
        session.sessionId = id;
        events.push({ kind: "session_started", sessionId: id });
        log.info("agent.session_started", { runner: "claude-code", session_id: id });
      },
      onAssistantText: (text) => {
        events.push({ kind: "assistant_text", text });
      },
      onToolUse: (name) => {
        events.push({ kind: "tool_use", name });
        log.debug("agent.tool_use", { runner: "claude-code", name });
      },
      onResult: (res) => {
        usage = res.usage ?? usage;
        resultMessage = res.result;
        resultIsError = !!res.is_error;
        events.push({
          kind: "turn_completed",
          usage: res.usage,
          result: res.result,
          sessionId: res.session_id ?? sessionId ?? undefined,
        });
        if (res.session_id) {
          sessionId = res.session_id;
          session.sessionId = res.session_id;
        }
      },
      onOther: (type) => {
        events.push({ kind: "other_message", type });
      },
    });
  });

  const code: number = await new Promise((resolveExit) => {
    child.once("close", (c) => resolveExit(c ?? 0));
    child.once("error", () => resolveExit(1));
  });
  clearTimeout(stallTimer);
  clearTimeout(turnTimer);
  abort.removeEventListener("abort", onAbort);

  if (exitReason === "stalled") {
    return { ok: false, reason: "stalled", sessionId, usage, events };
  }
  if (exitReason === "turn_timeout") {
    return { ok: false, reason: "turn_timeout", sessionId, usage, events };
  }
  if (exitReason === "canceled") {
    return { ok: false, reason: "turn_cancelled", sessionId, usage, events };
  }
  const stderrStr = stderrChunks.join("");
  const resMsgLower = resultMessage ? resultMessage.toLowerCase() : "";
  const stdErrLower = stderrStr.toLowerCase();
  
  const malformedHasRateLimit = events.some(e => 
    e.kind === "malformed" && 
    (e.line.toLowerCase().includes('rate limit') || 
     e.line.toLowerCase().includes('out of credits') ||
     e.line.toLowerCase().includes('insufficient_quota') ||
     e.line.toLowerCase().includes('hit your limit'))
  );
  
  const isRateLimit = 
    (resMsgLower.includes('rate_limit') || resMsgLower.includes('hit your limit') || resMsgLower.includes('insufficient_quota') || resMsgLower.includes('out of credits')) ||
    (stdErrLower.includes('rate_limit') || stdErrLower.includes('hit your limit') || stdErrLower.includes('insufficient_quota') || stdErrLower.includes('out of credits')) ||
    malformedHasRateLimit;

  if (isRateLimit) {
    return {
      ok: false,
      reason: "rate_limit",
      sessionId,
      usage,
      events,
    };
  }

  if (resultIsError) {
    return {
      ok: false,
      reason: `turn_failed: ${resultMessage ?? "unknown"}`,
      sessionId,
      usage,
      events,
    };
  }
  if (code !== 0) {
    return {
      ok: false,
      reason: `agent_exit_code=${code}: ${stderrStr.slice(0, 500)}`,
      sessionId,
      usage,
      events,
    };
  }
  return { ok: true, sessionId, usage, events };
}

interface StreamHandlers {
  onSession: (id: string) => void;
  onAssistantText: (text: string) => void;
  onToolUse: (name: string) => void;
  onResult: (res: {
    usage?: Usage;
    result?: string;
    is_error?: boolean;
    session_id?: string;
  }) => void;
  onOther: (type: string) => void;
}

function handleStreamEvent(obj: unknown, h: StreamHandlers): void {
  if (!obj || typeof obj !== "object") return;
  const o = obj as Record<string, unknown>;
  const type = typeof o.type === "string" ? o.type : "unknown";

  if (type === "system" && o.subtype === "init") {
    if (typeof o.session_id === "string") h.onSession(o.session_id);
    return;
  }
  if (type === "assistant" || type === "user") {
    const msg = o.message as { content?: unknown[] } | undefined;
    for (const part of msg?.content ?? []) {
      if (!part || typeof part !== "object") continue;
      const p = part as Record<string, unknown>;
      if (p.type === "text" && typeof p.text === "string") h.onAssistantText(p.text);
      else if (p.type === "tool_use" && typeof p.name === "string") h.onToolUse(p.name);
    }
    return;
  }
  if (type === "result") {
    const rawUsage = (o.usage as Record<string, number> | undefined) ?? undefined;
    const usage: Usage | undefined = rawUsage
      ? {
          input_tokens: rawUsage.input_tokens,
          output_tokens: rawUsage.output_tokens,
          total_tokens:
            rawUsage.total_tokens ??
            ((rawUsage.input_tokens ?? 0) + (rawUsage.output_tokens ?? 0)),
        }
      : undefined;
    h.onResult({
      usage,
      result: typeof o.result === "string" ? o.result : undefined,
      is_error: o.is_error === true || o.subtype === "error",
      session_id: typeof o.session_id === "string" ? o.session_id : undefined,
    });
    return;
  }
  if (type === "system" && (o.subtype === "hook_started" || o.subtype === "hook_response")) {
    return;
  }
  h.onOther(type);
}
