// Runner registry — single dispatch point. The orchestrator asks
// `getRunner(cfg)` once per turn and gets back the function it should call.
//
// Selection rule:
//   cfg.agent.runner === "aider"        -> aider-runner
//   cfg.agent.runner === "acp"          -> acp-runner (stub for now)
//   cfg.agent.runner === "claude-code"  -> claude-code-runner (default)
//   undefined / unset                   -> claude-code-runner (backwards-compat)
//
// Adding a new runner is a four-line change here plus the runner module.

import type { Runner } from "./runner.js";
import type { WorkflowConfig } from "../types.js";
import { runClaudeCodeTurn } from "./claude-code-runner.js";
import { runAiderTurn } from "./aider-runner.js";
import { runAcpTurn } from "./acp-runner.js";
import { runLanggraphTurn } from "./langgraph-runner.js";
import { FailoverManager } from "./failover.js";
import { runFallbackPipeline } from "./fallback-runner.js";

// The orchestrator has a single process lifecycle per harness.
// This global failover manager keeps state of the rate limits across turns and issues.
const failoverManager = new FailoverManager(runClaudeCodeTurn, runFallbackPipeline);

export type RunnerName = "claude-code" | "aider" | "acp" | "langgraph";

const RUNNERS: Record<RunnerName, Runner> = {
  "claude-code": (opts) => failoverManager.run(opts),
  aider: runAiderTurn,
  acp: runAcpTurn,
  langgraph: runLanggraphTurn,
};

export function getRunner(cfg: WorkflowConfig): { name: RunnerName; runner: Runner } {
  const name = (cfg.agent.runner ?? "claude-code") as RunnerName;
  const runner = RUNNERS[name];
  if (!runner) {
    // Should be unreachable given the zod schema validation on WorkflowConfig,
    // but defend at the boundary so a bad config gives a useful error rather
    // than a silent crash deep in the orchestrator.
    throw new Error(
      `unknown runner "${name}". Valid options: ${Object.keys(RUNNERS).join(", ")}`,
    );
  }
  return { name, runner };
}

/** For dashboards and telemetry — fast lookup of the runner name only. */
export function runnerNameFor(cfg: WorkflowConfig): RunnerName {
  return (cfg.agent.runner ?? "claude-code") as RunnerName;
}

export function getFailoverState() {
  return failoverManager.getState();
}
