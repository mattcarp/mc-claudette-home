// ACP runner — speaks the Agent Client Protocol (acp.dev) over stdio to an
// external agent process. ACP 1.0 is the open standard introduced by Zed in
// 2026 for editor⇄agent communication. Reference implementations exist for
// Gemini CLI, OpenAI Codex CLI, Claude Agent, and GitHub Copilot.
//
// STATUS: STUB. The wire protocol surface is real (typed request/response
// envelopes, capability negotiation, streaming notifications), but a complete
// JSON-RPC 2.0 client implementation is more than a one-night refactor.
// Phase 1 ships the typed stub so the runner registry can dispatch by name
// today; the actual protocol implementation is a follow-up MAG ticket.
//
// Why ship a stub vs delaying the whole multi-runner change:
//   - The runner abstraction is the load-bearing piece, and that's complete.
//   - Aider runner gives us real multi-model coverage (DeepSeek/Gemini/GPT/
//     Grok) without ACP — so the failover ladder works before ACP lands.
//   - When ACP support arrives, it slots in cleanly. No further refactoring
//     of the orchestrator or registry needed; only this file gains a real
//     body.
//
// What a real ACP runner would do (for the follow-up implementer):
//   1. spawn the agent binary (e.g. `gemini` for Gemini CLI in ACP mode,
//      `claude --acp`, `codex --acp`, or whatever the agent's flag is)
//   2. perform the capability handshake over stdin/stdout JSON-RPC 2.0:
//      → initialize  (declares client capabilities, requested protocol version)
//      ← initialized (agent responds with its capabilities + version)
//   3. authenticate if the agent requires it (most CLIs delegate auth to env)
//   4. open a session and post the user prompt as a `session/prompt` request
//   5. stream incoming notifications:
//        session/update  → assistant_text, tool_use, etc.
//        session/cancel  → turn_cancelled
//        session/error   → turn_failed
//        session/complete → turn_completed (with usage)
//   6. translate everything into AgentEvent shape, bubble usage up
//   7. on cancellation/timeout: send `session/cancel` then SIGTERM
//
// Spec home: https://zed.dev/acp ; reference impls under github.com/zed-industries
// /agent-client-protocol and the Helix.ml fork blog post linked from there.

import type { AgentRunResult, RunOptions } from "./runner.js";

export async function runAcpTurn(opts: RunOptions): Promise<AgentRunResult> {
  const { log, cfg } = opts;
  const acp = cfg.acp;
  log.warn("agent.acp_not_implemented", {
    requested_agent: acp?.agent ?? "(unset)",
    requested_model: cfg.agent.model ?? "(unset)",
    note: "ACP runner is a stub; falling back caller should pick a different runner",
  });
  return {
    ok: false,
    reason:
      "acp_not_implemented: the ACP runner is a typed stub awaiting full JSON-RPC 2.0 protocol implementation. Use runner: claude-code or runner: aider for now.",
    sessionId: null,
    usage: {},
    events: [],
  };
}
