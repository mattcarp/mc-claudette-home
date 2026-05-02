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

export const AgentConfigSchema = z.object({
  max_concurrent_agents: z.number().int().positive().default(3),
  max_concurrent_agents_by_state: z.record(z.string(), z.number().int().positive()).optional(),
  max_turns: z.number().int().positive().default(20),
  max_retry_backoff_ms: z.number().int().positive().default(300_000),
});

export const CodexConfigSchema = z.object({
  command: z.string().default(
    "claude -p --output-format=stream-json --verbose --permission-mode=acceptEdits"
  ),
  turn_timeout_ms: z.number().int().positive().default(3_600_000),
  read_timeout_ms: z.number().int().positive().default(5_000),
  stall_timeout_ms: z.number().int().positive().default(300_000),
});

export const ServerConfigSchema = z
  .object({
    port: z.number().int().positive().optional(),
    host: z.string().default("127.0.0.1"),
  })
  .optional();

export const WorkflowConfigSchema = z.object({
  tracker: TrackerConfigSchema,
  polling: PollingConfigSchema.default({ interval_ms: 30_000 }),
  workspace: WorkspaceConfigSchema.default({ root: "~/symphony_workspaces" }),
  hooks: HooksConfigSchema.default({ timeout_ms: 60_000 }),
  agent: AgentConfigSchema.default({
    max_concurrent_agents: 3,
    max_turns: 20,
    max_retry_backoff_ms: 300_000,
  }),
  codex: CodexConfigSchema.default({
    command: "claude -p --output-format=stream-json --verbose --permission-mode=acceptEdits",
    turn_timeout_ms: 3_600_000,
    read_timeout_ms: 5_000,
    stall_timeout_ms: 300_000,
  }),
  server: ServerConfigSchema,
});

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
