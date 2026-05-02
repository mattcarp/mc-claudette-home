import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { createInterface } from "node:readline";
import type { WorkflowConfig, Issue, RunningSession } from "../types.js";
import type { Logger } from "../logger.js";

export type AgentEvent =
  | { kind: "session_started"; sessionId: string }
  | { kind: "turn_completed"; usage?: Usage; result?: string; sessionId?: string }
  | { kind: "turn_failed"; reason: string }
  | { kind: "turn_cancelled"; reason: string }
  | { kind: "stalled"; elapsedMs: number }
  | { kind: "tool_use"; name: string }
  | { kind: "assistant_text"; text: string }
  | { kind: "rate_limit"; payload: unknown }
  | { kind: "other_message"; type: string }
  | { kind: "malformed"; line: string };

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
}

function shellQuote(s: string): string {
  if (/^[A-Za-z0-9_./:-]+$/.test(s)) return s;
  return `'${s.replace(/'/g, "'\\''")}'`;
}

export interface AgentRunResult {
  ok: boolean;
  reason?: string;
  sessionId: string | null;
  usage: Usage;
  events: AgentEvent[];
}

export interface RunOptions {
  cwd: string;
  prompt: string;
  resumeSessionId: string | null;
  log: Logger;
  abort: AbortSignal;
  session: RunningSession;
  cfg: WorkflowConfig;
}

/**
 * Runs one Claude Code turn in headless stream-json mode. Per spec:
 *   Command: bash -lc <codex.command>
 *   Working directory: per-issue workspace path
 *   Transport: stdio JSON lines
 * - stdin closes after we send the prompt (single-shot, like Codex turn)
 * - stdout emits one JSON object per line (system, assistant, result, etc.)
 * - stall_timeout_ms triggers SIGTERM if no event arrives
 * - turn_timeout_ms triggers SIGTERM regardless
 */
export async function runAgentTurn(opts: RunOptions): Promise<AgentRunResult> {
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

  // Sanitize env — strip anything that could override Claude Code's OAuth credentials
  // and force it onto an "external" API key path. Workshop and similar machines often
  // have ANTHROPIC_API_KEY set somewhere upstream; if present, claude prefers it over
  // the local OAuth token in ~/.claude/.credentials.json and the API rejects it.
  const cleanEnv: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined) continue;
    if (/^(ANTHROPIC_|CLAUDE_API|CLAUDE_CODE_API)/i.test(k)) continue;
    cleanEnv[k] = v;
  }

  log.info("agent.spawn", { cmd: fullCmd.replace(/\s+/g, " ").slice(0, 200), cwd });

  let child: ChildProcessByStdio<Writable, Readable, Readable>;
  try {
    child = spawn("bash", ["-lc", fullCmd], {
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

  // Send the prompt and close stdin
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
      log.warn("agent.stalled", { stallMs, since_last_event_ms: Date.now() - session.lastEventAt });
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
        log.info("agent.session_started", { session_id: id });
      },
      onAssistantText: (text) => {
        events.push({ kind: "assistant_text", text });
      },
      onToolUse: (name) => {
        events.push({ kind: "tool_use", name });
        log.debug("agent.tool_use", { name });
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
  if (resultIsError) {
    return { ok: false, reason: `turn_failed: ${resultMessage ?? "unknown"}`, sessionId, usage, events };
  }
  if (code !== 0) {
    return {
      ok: false,
      reason: `agent_exit_code=${code}: ${stderrChunks.join("").slice(0, 500)}`,
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
  onResult: (res: { usage?: Usage; result?: string; is_error?: boolean; session_id?: string }) => void;
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
  // Hook noise — silence the firehose at session start
  if (type === "system" && (o.subtype === "hook_started" || o.subtype === "hook_response")) {
    return;
  }
  h.onOther(type);
}
