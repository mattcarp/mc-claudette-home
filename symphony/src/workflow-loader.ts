import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import YAML from "yaml";
import { Liquid } from "liquidjs";
import { WorkflowConfigSchema, type Workflow, type WorkflowConfig, type Issue } from "./types.js";

const FRONT_MATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/;

const liquid = new Liquid({ strictFilters: true, strictVariables: true });

function resolveEnvVars(node: unknown): unknown {
  if (typeof node === "string") {
    if (node.startsWith("$")) {
      const key = node.slice(1);
      const v = process.env[key];
      if (v === undefined) {
        throw new Error(`environment variable ${key} referenced in workflow but not set`);
      }
      return v;
    }
    return node;
  }
  if (Array.isArray(node)) return node.map(resolveEnvVars);
  if (node && typeof node === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(node)) out[k] = resolveEnvVars(v);
    return out;
  }
  return node;
}

export async function loadWorkflow(path: string): Promise<Workflow> {
  const abs = resolve(path);
  const raw = await readFile(abs, "utf8");
  const m = raw.match(FRONT_MATTER_RE);
  let configRaw: unknown = {};
  let body = raw;
  if (m) {
    configRaw = YAML.parse(m[1]!) ?? {};
    body = m[2] ?? "";
  }
  const resolved = resolveEnvVars(configRaw);
  const config: WorkflowConfig = WorkflowConfigSchema.parse(resolved);
  const promptTemplate =
    body.trim().length > 0
      ? body
      : "You are working on {{ issue.identifier }}: {{ issue.title }}.\n\n{{ issue.description }}";
  return { config, promptTemplate, sourcePath: abs };
}

export async function renderPrompt(
  template: string,
  issue: Issue,
  attempt: number | null,
): Promise<string> {
  return liquid.parseAndRender(template, { issue, attempt });
}
