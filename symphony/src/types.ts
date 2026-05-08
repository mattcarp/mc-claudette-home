import { z } from "zod";

export const TrackerConfigSchema = z
  .object({
    kind: z.literal("linear"),
    api_key: z.string().min(1),
    project_slug: z.string().optional(),
    team_key: z.string().optional(),
    active_states: z.array(z.string()).default(["Todo", "In Progress"]),
    terminal_states: z.array(z.string()).default(["Done", "Canceled", "Cancelled"]),
    exclude_labels: z.array(z.string()).default([]),
    // If non-empty, only issues that have at least one of these labels (case-insensitive)
    // are candidates. Use with a sibling harness that excludes the same labels so the
    // Linear queue is partitioned (e.g. workshop MAG vs Mac satellite for CAD/SIAM).
    require_any_labels: z.array(z.string()).default([]),
  })
  .refine((v) => !!(v.project_slug || v.team_key), {
    message: "tracker requires either project_slug or team_key",
  });

export const PollingConfigSchema = z.object({
  interval_ms: z.number().int().positive().default(30_000),
});

export const WorkspaceConfigSchema = z.object({
  root: z.string().default("~/symphony_workspaces"),
});

export const HooksConfigSchema = z.object({
  after_create: z.string().optional(),
  before_run: z.string().optional(),
  after_run: z.string().optional(),
  before_remove: z.string().optional(),
  timeout_ms: z.number().int().positive().default(60_000),
});

// Runner names recognized by symphony/src/agent/registry.ts. Adding a runner
// is a four-line change in registry.ts plus the runner module; remember to
// extend this enum so zod validation accepts the new name.
export const RunnerNameSchema = z.enum(["claude-code", "aider", "acp", "langgraph"]);

// One tier of the failover ladder. The orchestrator reads tiers in order;
// when a tier's threshold trips, it switches the active runner+model to
// the next tier's choice. Phase 2 implements the threshold detector that
// drives this; Phase 1 ships the schema so WORKFLOW.md doesn't migrate twice.
export const FallbackTierSchema = z.object({
  // 0–100. Anthropic-Max % used at which this tier becomes active. The
  // detector is responsible for figuring out the actual %; sources include
  // a reverse-engineered claude.ai endpoint or, as a sandbag fallback, the
  // harness's own token-sum proxy.
  threshold_percent: z.number().min(0).max(100),
  runner: RunnerNameSchema,
  model: z.string().optional(),
  // Optional inline review pass: a SECOND model checks the implementer's
  // output before commit. Cheap insurance against confabulation when the
  // implementer is on a less-trusted family. See "Pattern B" in the proposal.
  reviewer: z
    .object({
      runner: RunnerNameSchema,
      model: z.string(),
    })
    .optional(),
});

export const AgentConfigSchema = z.object({
  max_concurrent_agents: z.number().int().positive().default(3),
  max_concurrent_agents_by_state: z.record(z.string(), z.number().int().positive()).optional(),
  max_turns: z.number().int().positive().default(20),
  max_retry_backoff_ms: z.number().int().positive().default(300_000),
  // Multi-runner additions:
  runner: RunnerNameSchema.default("claude-code"),
  // Model name passed to the runner. Meaning is runner-specific:
  //   claude-code: ignored (model is in cfg.codex.command)
  //   aider:       passed as `--model <value>` (e.g. "deepseek/deepseek-v4-pro",
  //                "gemini/gemini-3.1-pro", "openai/gpt-5.5", "xai/grok-4.3")
  //   acp:         (when implemented) used to negotiate the agent's model
  model: z.string().optional(),
  // Failover ladder. Tiers are evaluated in array order; first whose threshold
  // condition trips becomes active. An empty array means "no failover", run
  // the primary runner until quota fully exhausts and tickets stall.
  fallback: z.array(FallbackTierSchema).default([]),
});

export const CodexConfigSchema = z.object({
  command: z.string().default(
    "claude -p --output-format=stream-json --verbose --permission-mode=acceptEdits"
  ),
  turn_timeout_ms: z.number().int().positive().default(3_600_000),
  read_timeout_ms: z.number().int().positive().default(5_000),
  stall_timeout_ms: z.number().int().positive().default(300_000),
});

// Aider runner config. Sibling to `codex:`. Only consulted when
// `agent.runner === "aider"`.
export const AiderConfigSchema = z.object({
  command: z.string().default("aider"),
  // If unset we use a sane default: --no-stream --no-pretty --yes (one-shot
  // headless). Override only if the project needs different flags.
  extra_args: z.array(z.string()).optional(),
  turn_timeout_ms: z.number().int().positive().default(3_600_000),
  stall_timeout_ms: z.number().int().positive().default(300_000),
  // When true, we pass --show-resource-usage and parse the resulting line
  // for token counts. Off by default because the line shape is mildly
  // version-dependent and a parse miss isn't worth a noisy log.
  show_resource_usage: z.boolean().default(true),
});

// ACP runner config. Sibling to `codex:`. Stub for now — body lands in a
// follow-up ticket. See symphony/src/agent/acp-runner.ts.
export const AcpConfigSchema = z.object({
  // Which ACP-compatible agent binary to launch. e.g. "gemini", "codex",
  // "claude --acp". The runner spawns this and speaks JSON-RPC 2.0 over
  // stdio per the ACP 1.0 spec at https://zed.dev/acp.
  agent: z.string(),
  extra_args: z.array(z.string()).optional(),
  turn_timeout_ms: z.number().int().positive().default(3_600_000),
  stall_timeout_ms: z.number().int().positive().default(300_000),
});

export const LanggraphExecutionModeSchema = z.enum(["planner-only", "turn"]);

export const LanggraphConfigSchema = z.object({
  command: z.string().default("python symphony/src/agent/langgraph/main.py"),
  turn_timeout_ms: z.number().int().positive().default(3_600_000),
  stall_timeout_ms: z.number().int().positive().default(600_000),
  max_workers: z.number().int().positive().default(3),
  execution_mode: LanggraphExecutionModeSchema.default("planner-only"),
  /** After this many consecutive LangGraph turns with worker_no_changes / empty diff, park the issue in In Review. */
  max_consecutive_worker_no_changes: z.number().int().positive().default(3),
  /**
   * Optional bash script run once per worker session (issue workspace cwd) before the agent turn.
   * Env: SYMPHONY_ISSUE_IDENTIFIER, SYMPHONY_ISSUE_ID, SYMPHONY_ISSUE_TITLE, SYMPHONY_REPO_ROOT.
   * Exit 0 = continue. Exit 77 = skip agent — issue moved to In Review with stderr in comment.
   */
  pre_dispatch_shell: z.string().optional(),
});

export const ServerConfigSchema = z
  .object({
    port: z.number().int().positive().optional(),
    host: z.string().default("127.0.0.1"),
  })
  .optional();

// Posture controls how aggressive the agent is allowed to be on this project.
// "greenfield" — agents may push commits if the issue is labeled `auto-push`.
// "production" — agents must NEVER push, regardless of labels. Commits stay
//                local on the symphony/<ID> branch for human PR review.
export const PostureSchema = z.enum(["greenfield", "production"]).default("greenfield");

export const WorkflowConfigSchema = z
  .object({
    tracker: TrackerConfigSchema,
    posture: PostureSchema,
    polling: PollingConfigSchema.default({ interval_ms: 30_000 }),
    workspace: WorkspaceConfigSchema.default({ root: "~/symphony_workspaces" }),
    hooks: HooksConfigSchema.default({ timeout_ms: 60_000 }),
    agent: AgentConfigSchema.default({
      max_concurrent_agents: 3,
      max_turns: 20,
      max_retry_backoff_ms: 300_000,
      runner: "claude-code",
      fallback: [],
    }),
    codex: CodexConfigSchema.default({
      command: "claude -p --output-format=stream-json --verbose --permission-mode=acceptEdits",
      turn_timeout_ms: 3_600_000,
      read_timeout_ms: 5_000,
      stall_timeout_ms: 300_000,
    }),
    // aider: and acp: blocks are optional — only consulted when
    // agent.runner picks them.
    aider: AiderConfigSchema.optional(),
    acp: AcpConfigSchema.optional(),
    langgraph: LanggraphConfigSchema.optional(),
    server: ServerConfigSchema,
  })
  // .strict() so unknown top-level keys fail loud at load time. Without this,
  // zod silently strips them — which is how the `posture` field went missing
  // from mc-siam after the field was added to canonical. Audit at flip time:
  // every WORKFLOW.md across the eight projects used only known top-level
  // keys, so this is safe. If a future schema rename lands and the
  // per-project WORKFLOW.md hasn't been updated yet, the harness will refuse
  // to start with a clear error pointing at the unknown field — that is the
  // point.
  .strict();

export type WorkflowConfig = z.infer<typeof WorkflowConfigSchema>;

export interface Workflow {
  config: WorkflowConfig;
  promptTemplate: string;
  sourcePath: string;
}

export interface Issue {
  id: string;
  identifier: string;
  title: string;
  description: string | null;
  state: string;
  priority: number | null;
  labels: string[];
  blocked_by: string[];
  url: string;
  created_at: string;
  updated_at: string;
}

export type RunPhase =
  | "PreparingWorkspace"
  | "BuildingPrompt"
  | "LaunchingAgentProcess"
  | "InitializingSession"
  | "StreamingTurn"
  | "Finishing"
  | "Succeeded"
  | "Failed"
  | "TimedOut"
  | "Stalled"
  | "CanceledByReconciliation";

export interface RunningSession {
  issueId: string;
  identifier: string;
  workspacePath: string;
  startedAt: number;
  lastEventAt: number;
  turnCount: number;
  phase: RunPhase;
  sessionId: string | null;
  abort: AbortController;
  totals: { input: number; output: number; total: number };
}

export interface RetryEntry {
  issueId: string;
  identifier: string;
  attempt: number;
  dueAt: number;
  lastError: string;
  timer: NodeJS.Timeout;
}
