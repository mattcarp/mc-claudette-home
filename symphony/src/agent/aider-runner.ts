// Aider runner — spawns `aider --message-file` for a single one-shot turn.
// Adapts Aider's plain-text output into the AgentEvent shape the rest of
// the harness expects.
//
// Why Aider as a second runner:
//   - genuinely model-agnostic: routes via --model deepseek/..., gemini/...,
//     openai/..., xai/grok-..., anthropic/... etc. Same CLI, different brains.
//   - mature, single-maintainer, MIT-licensed; protocol surface (stdin prompt,
//     git commits as artifacts) is stable across years.
//   - no proprietary stream-json shape to track upstream changes for.
//
// Semantic differences vs Claude Code worth knowing:
//   - No session concept. Every invocation is a fresh process. We synthesize
//     a session_started event with a generated UUID for symmetry. `resume`
//     is a no-op — the next turn just runs again on the same workspace and
//     sees the prior commits in the git history.
//   - Aider auto-commits its changes. The prompt template still needs to
//     instruct the LLM to include `[symphony:done]` in the commit message;
//     the message is LLM-generated, so the instruction lands.
//   - No tool-use events with names. We synthesize tool_use{name:"Edit"}
//     when we see "Applied edit to <FILE>" lines so the dashboard's
//     tool-use stream stays populated.
//
// Config (in WORKFLOW.md, sibling to `codex:`):
//
//   agent:
//     runner: aider
//     model: deepseek/deepseek-v4-pro
//
//   aider:
//     command: aider          # binary; path or just `aider` if on PATH
//     extra_args: ["--no-stream", "--no-pretty", "--yes"]   # optional
//     turn_timeout_ms: 3600000
//     stall_timeout_ms: 300000
//     show_resource_usage: true   # parse token-usage line from stderr
//
// API keys come from the environment (Infisical-injected). Aider reads
// DEEPSEEK_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY / XAI_API_KEY /
// ANTHROPIC_API_KEY based on the --model prefix. We pass them through.

import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { createInterface } from "node:readline";
import { writeFile, unlink, mkdtemp, stat, readFile, mkdir, chmod } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { AgentEvent, AgentRunResult, RunOptions, Usage } from "./runner.js";
import { refusingPrePushHook } from "./posture.js";

// Aider has no allowlist surface — the model just shells whatever it wants.
// For posture=production we install a refusing pre-push git hook in the
// worker's .git directory before spawn. Hooks-level block is the floor;
// the prompt-body "no auto-push" rule is the ceiling.
async function installRefusingPrePushHook(workspaceCwd: string): Promise<void> {
  const dotgit = join(workspaceCwd, ".git");
  let gitdir: string;
  const st = await stat(dotgit);
  if (st.isDirectory()) {
    gitdir = dotgit;
  } else {
    // Worktree case: .git is a file containing "gitdir: <path>".
    const contents = await readFile(dotgit, "utf8");
    const m = contents.match(/^gitdir:\s*(.+)$/m);
    if (!m) throw new Error(`unexpected .git format: ${contents.slice(0, 200)}`);
    gitdir = m[1]!.trim();
  }
  const hooksDir = join(gitdir, "hooks");
  await mkdir(hooksDir, { recursive: true });
  const hookPath = join(hooksDir, "pre-push");
  await writeFile(hookPath, refusingPrePushHook(), "utf8");
  await chmod(hookPath, 0o755);
}

const APPLIED_EDIT_RE = /^Applied edit to (.+)$/;
// Aider's --show-resource-usage line:
//   "Tokens: 1.2k sent, 800 received. Cost: $0.00 message, $0.01 session."
const TOKENS_RE = /Tokens:\s*([\d.]+)([km]?)\s+sent,\s*([\d.]+)([km]?)\s+received/i;

export function signalTargets(pid: number | undefined, signal: NodeJS.Signals): Array<{ pid: number; signal: NodeJS.Signals }> {
  if (!pid) return [];
  // Aider can spawn helpers (including npm/npx tools). Running it as a process
  // group lets cancellation reap the whole tree instead of leaving hot orphans.
  return [{ pid: -pid, signal }];
}

function parseScaled(num: string, suffix: string): number {
  const n = parseFloat(num);
  if (Number.isNaN(n)) return 0;
  const mult = suffix.toLowerCase() === "k" ? 1_000 : suffix.toLowerCase() === "m" ? 1_000_000 : 1;
  return Math.round(n * mult);
}

export async function runAiderTurn(opts: RunOptions): Promise<AgentRunResult> {
  const { cwd, prompt, log, abort, session, cfg } = opts;
  const aiderCfg = cfg.aider;
  const model = cfg.agent.model;

  if (!model) {
    return {
      ok: false,
      reason: "aider_misconfigured: agent.model is required for runner=aider",
      sessionId: null,
      usage: {},
      events: [],
    };
  }
  if (!aiderCfg) {
    return {
      ok: false,
      reason: "aider_misconfigured: aider config block missing in WORKFLOW.md",
      sessionId: null,
      usage: {},
      events: [],
    };
  }

  // Write prompt to a temp file. --message-file dodges argv length limits and
  // shell-quoting headaches for prompts with backticks, dollar signs, etc.
  const tmpDir = await mkdtemp(join(tmpdir(), "aider-prompt-"));
  const promptPath = join(tmpDir, "prompt.md");
  await writeFile(promptPath, prompt, "utf8");

  // Default flags chosen for unattended one-shot dispatch:
  //   --no-stream              don't try to render token-by-token (we capture lines)
  //   --no-pretty              plain output; we parse it
  //   --yes-always             auto-accept all confirmations (no interactive Y/n)
  //   --no-check-update        don't ping pypi mid-dispatch
  //   --no-show-release-notes  suppress first-run-after-upgrade notes
  //   --no-analytics           don't prompt for opt-in on first run
  //   --no-fancy-input         we use --message-file, not stdin, so no fancy UI needed
  // These can be overridden by setting `aider.extra_args` in WORKFLOW.md.
  const args = [
    "--message-file",
    promptPath,
    "--model",
    model,
    "--no-show-model-warnings",
    ...(aiderCfg.extra_args ?? [
      "--no-stream",
      "--no-pretty",
      "--yes-always",
      "--no-check-update",
      "--no-show-release-notes",
      "--no-analytics",
      "--no-fancy-input",
    ]),
  ];
  // Note: Aider 0.86.2 does not expose --show-resource-usage. Token usage in
  // events is best-effort: if a future Aider release reintroduces the flag
  // (or another way to surface counts), the regex in stderr handling will
  // start populating numbers. For now, usage stays at zero for aider runs.

  const cmdBin = (aiderCfg.command ?? "aider").trim() || "aider";

  log.info("agent.spawn", {
    runner: "aider",
    cmd: `${cmdBin} ${args.slice(0, 6).join(" ")}…`,
    model,
    cwd,
  });

  // Synthetic session id — Aider has no native session, but the rest of the
  // harness expects one for joining log lines and dashboard rows.
  const synthSessionId = `aider-${randomUUID()}`;

  const events: AgentEvent[] = [];
  events.push({ kind: "session_started", sessionId: synthSessionId });
  session.sessionId = synthSessionId;
  log.info("agent.session_started", { runner: "aider", session_id: synthSessionId });

  if (cfg.posture === "production") {
    try {
      await installRefusingPrePushHook(cwd);
      log.info("posture.pre_push_hook_installed", { posture: "production", cwd });
    } catch (err) {
      // Don't fail the run for this — prompt-level rules still apply. But
      // surface it loudly so an operator notices the floor isn't installed.
      log.warn("posture.pre_push_hook_failed", { error: String(err), cwd });
    }
  }

  let child: ChildProcessByStdio<Writable, Readable, Readable>;
  try {
    child = spawn(cmdBin, args, {
      cwd,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
      detached: true,
    });
  } catch (err) {
    await safeCleanup(promptPath);
    return {
      ok: false,
      reason: `aider_not_found: ${String(err)}`,
      sessionId: synthSessionId,
      usage: {},
      events,
    };
  }
  // Close stdin immediately — we used --message-file, not stdin prompts.
  child.stdin.end();

  let exitReason: string | null = null;
  let usage: Usage = {};

  const killChildTree = (signal: NodeJS.Signals) => {
    for (const target of signalTargets(child.pid, signal)) {
      try {
        process.kill(target.pid, target.signal);
      } catch {
        child.kill(signal);
      }
    }
  };

  const stallMs = aiderCfg.stall_timeout_ms;
  const turnMs = aiderCfg.turn_timeout_ms;

  let stallTimer: NodeJS.Timeout | undefined;
  const armStall = () => {
    if (stallMs <= 0) return;
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      exitReason = exitReason ?? "stalled";
      events.push({ kind: "stalled", elapsedMs: Date.now() - session.lastEventAt });
      log.warn("agent.stalled", {
        runner: "aider",
        stallMs,
        since_last_event_ms: Date.now() - session.lastEventAt,
      });
      killChildTree("SIGTERM");
      setTimeout(() => killChildTree("SIGKILL"), 5000).unref();
    }, stallMs);
    stallTimer.unref();
  };
  armStall();

  const turnTimer = setTimeout(() => {
    exitReason = exitReason ?? "turn_timeout";
    log.warn("agent.turn_timeout", { runner: "aider", turnMs });
    killChildTree("SIGTERM");
    setTimeout(() => killChildTree("SIGKILL"), 5000).unref();
  }, turnMs);
  turnTimer.unref();

  const onAbort = () => {
    exitReason = exitReason ?? "canceled";
    killChildTree("SIGTERM");
    setTimeout(() => killChildTree("SIGKILL"), 5000).unref();
  };
  if (abort.aborted) onAbort();
  else abort.addEventListener("abort", onAbort, { once: true });

  const stderrChunks: string[] = [];
  const stdoutChunks: string[] = [];
  // Aider writes its model/repo banner + the resource-usage line to stderr.
  // We stream stderr line-by-line to scrape token usage; cap retained chunks
  // so a chatty model doesn't balloon memory.
  const stderrRl = createInterface({ input: child.stderr, crlfDelay: Infinity });
  stderrRl.on("line", (line) => {
    if (stderrChunks.length < 200) stderrChunks.push(line);
    if (line.trim()) {
      session.lastEventAt = Date.now();
      armStall();
    }
    const m = line.match(TOKENS_RE);
    if (m) {
      const sent = parseScaled(m[1], m[2]);
      const recv = parseScaled(m[3], m[4]);
      usage = {
        input_tokens: (usage.input_tokens ?? 0) + sent,
        output_tokens: (usage.output_tokens ?? 0) + recv,
        total_tokens: (usage.total_tokens ?? 0) + sent + recv,
      };
    }
  });

  const stdoutRl = createInterface({ input: child.stdout, crlfDelay: Infinity });
  stdoutRl.on("line", (line) => {
    if (!line.trim()) return;
    if (stdoutChunks.length < 200) stdoutChunks.push(line);
    session.lastEventAt = Date.now();
    armStall();
    const editMatch = line.match(APPLIED_EDIT_RE);
    if (editMatch) {
      events.push({ kind: "tool_use", name: "Edit" });
      log.debug("agent.tool_use", { runner: "aider", name: "Edit", file: editMatch[1] });
      return;
    }
    // Aider's commit lines look like "Commit abc1234 …".
    if (/^Commit\s+[0-9a-f]{6,}/i.test(line)) {
      events.push({ kind: "assistant_text", text: line });
      return;
    }
    // Suppress everything else from the firehose; the orchestrator doesn't
    // care about Aider's prose, only its actions and outcome.
  });

  const code: number = await new Promise((resolveExit) => {
    child.once("close", (c) => resolveExit(c ?? 0));
    child.once("error", () => resolveExit(1));
  });
  clearTimeout(stallTimer);
  clearTimeout(turnTimer);
  abort.removeEventListener("abort", onAbort);
  await safeCleanup(promptPath);

  if (exitReason === "stalled") {
    return { ok: false, reason: "stalled", sessionId: synthSessionId, usage, events };
  }
  if (exitReason === "turn_timeout") {
    return { ok: false, reason: "turn_timeout", sessionId: synthSessionId, usage, events };
  }
  if (exitReason === "canceled") {
    return { ok: false, reason: "turn_cancelled", sessionId: synthSessionId, usage, events };
  }
  if (code !== 0) {
    return {
      ok: false,
      reason: `aider_exit_code=${code}: ${[...stderrChunks.slice(-10), ...stdoutChunks.slice(-10)].join(" / ").slice(0, 800)}`,
      sessionId: synthSessionId,
      usage,
      events,
    };
  }

  // Synthesize a turn_completed event so downstream parity holds.
  events.push({
    kind: "turn_completed",
    usage,
    sessionId: synthSessionId,
  });
  return { ok: true, sessionId: synthSessionId, usage, events };
}

async function safeCleanup(p: string): Promise<void> {
  try {
    await unlink(p);
  } catch {
    /* best-effort */
  }
}
