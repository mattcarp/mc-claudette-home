// Runner interface — the abstraction every concrete agent runtime implements.
//
// The orchestrator dispatches one turn at a time and doesn't care which agent
// runtime executes it. As long as a runner takes a prompt and produces a
// commit (or a failure reason) inside the workspace, it is interchangeable.
//
// Current concrete runners:
//   - claude-code-runner.ts: spawns `claude -p` (Anthropic Claude Code CLI)
//   - aider-runner.ts:       spawns `aider --message` (model-agnostic; DeepSeek,
//                            Gemini, GPT-5, Grok, etc.)
//   - acp-runner.ts:         (stub) speaks Agent Client Protocol over stdio,
//                            drives Gemini CLI, OpenAI Codex CLI, Claude Agent.
//
// The events surface (AgentEvent) is intentionally Claude-flavored historically
// — most non-Claude runners synthesize the matching events from their native
// output. That's a fine seam: every runner translates its native protocol into
// the same downstream event shape, so the orchestrator + dashboard + telemetry
// don't fork per runner.

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

export interface AgentRunResult {
  ok: boolean;
  reason?: string;
  sessionId: string | null;
  usage: Usage;
  events: AgentEvent[];
  usedFallback?: boolean;
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
 * The runner contract: take a turn's worth of options, return a result.
 * Pure function shape — the registry hands the orchestrator one of these
 * by name based on `cfg.agent.runner`.
 */
export type Runner = (opts: RunOptions) => Promise<AgentRunResult>;

// Re-export the Claude Code runner under its historical name so any external
// consumer of this module (or older code paths) keeps working unchanged.
// New code should go through registry.ts -> getRunner(cfg).
export { runClaudeCodeTurn as runAgentTurn } from "./claude-code-runner.js";
