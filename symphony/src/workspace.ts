import { mkdir, rm, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve, sep } from "node:path";
import { spawn } from "node:child_process";
import type { WorkflowConfig } from "./types.js";
import type { Logger } from "./logger.js";

export function sanitizeKey(identifier: string): string {
  return identifier.replace(/[^A-Za-z0-9._-]/g, "_");
}

function expandHome(p: string): string {
  if (p.startsWith("~/")) return join(homedir(), p.slice(2));
  if (p === "~") return homedir();
  return p;
}

export interface CreateResult {
  path: string;
  createdNow: boolean;
}

export class WorkspaceManager {
  private root: string;
  constructor(private cfg: WorkflowConfig, private log: Logger) {
    this.root = resolve(expandHome(cfg.workspace.root));
  }

  rootPath(): string {
    return this.root;
  }

  pathFor(identifier: string): string {
    const key = sanitizeKey(identifier);
    const p = resolve(this.root, key);
    // Path-traversal guard: must remain under root
    if (!p.startsWith(this.root + sep) && p !== this.root) {
      throw new Error(`workspace path escaped root: ${p}`);
    }
    return p;
  }

  async ensureRoot(): Promise<void> {
    await mkdir(this.root, { recursive: true });
  }

  async create(identifier: string): Promise<CreateResult> {
    const path = this.pathFor(identifier);
    let createdNow = false;
    try {
      const s = await stat(path);
      if (!s.isDirectory()) {
        throw new Error(`workspace path exists but is not a directory: ${path}`);
      }
    } catch (err: unknown) {
      const e = err as NodeJS.ErrnoException;
      if (e.code !== "ENOENT") throw err;
      await mkdir(path, { recursive: true });
      createdNow = true;
    }
    if (createdNow && this.cfg.hooks.after_create) {
      await this.runHook("after_create", this.cfg.hooks.after_create, path, identifier);
    }
    return { path, createdNow };
  }

  async remove(identifier: string): Promise<void> {
    const path = this.pathFor(identifier);
    if (this.cfg.hooks.before_remove) {
      try {
        await this.runHook("before_remove", this.cfg.hooks.before_remove, path, identifier);
      } catch (err) {
        this.log.warn("before_remove hook failed (ignored)", {
          identifier,
          err: String(err),
        });
      }
    }
    await rm(path, { recursive: true, force: true });
  }

  async runBeforeRun(identifier: string): Promise<void> {
    if (!this.cfg.hooks.before_run) return;
    const path = this.pathFor(identifier);
    await this.runHook("before_run", this.cfg.hooks.before_run, path, identifier);
  }

  async runAfterRun(identifier: string): Promise<void> {
    if (!this.cfg.hooks.after_run) return;
    const path = this.pathFor(identifier);
    try {
      await this.runHook("after_run", this.cfg.hooks.after_run, path, identifier);
    } catch (err) {
      this.log.warn("after_run hook failed (ignored)", { identifier, err: String(err) });
    }
  }

  private runHook(
    name: string,
    script: string,
    cwd: string,
    identifier: string,
  ): Promise<void> {
    const timeoutMs = this.cfg.hooks.timeout_ms;
    const log = this.log.child({ hook: name, identifier });
    return new Promise((resolveHook, rejectHook) => {
      // Hooks are repository-owned and trusted per spec; bash -lc is intentional.
      const child = spawn("bash", ["-lc", script], {
        cwd,
        env: { ...process.env, SYMPHONY_ISSUE_IDENTIFIER: identifier, SYMPHONY_WORKSPACE: cwd },
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      const cap = 4096;
      child.stdout.on("data", (chunk) => {
        if (stdout.length < cap) stdout += chunk.toString().slice(0, cap - stdout.length);
      });
      child.stderr.on("data", (chunk) => {
        if (stderr.length < cap) stderr += chunk.toString().slice(0, cap - stderr.length);
      });
      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        setTimeout(() => child.kill("SIGKILL"), 2000).unref();
      }, timeoutMs);
      child.once("error", (err) => {
        clearTimeout(timer);
        rejectHook(err);
      });
      child.once("close", (code, signal) => {
        clearTimeout(timer);
        if (code === 0) {
          log.info("hook ok", { stdout_truncated: stdout.length === cap });
          resolveHook();
        } else {
          log.error("hook failed", { code, signal, stderr: stderr.slice(0, 1024) });
          rejectHook(new Error(`hook ${name} exited code=${code} signal=${signal}`));
        }
      });
    });
  }
}
