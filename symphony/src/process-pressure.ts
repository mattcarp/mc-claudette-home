import { execFile } from "node:child_process";
import { freemem, loadavg } from "node:os";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const DEFAULT_RISKY_PATTERN =
  "task-master-ai|/aider|/home/.*/aider|claude -p|chrome-headless|remotion render";

export type ProcessPressure = {
  checkedAt: number;
  load1: number;
  freeMemMb: number;
  riskyProcessCount: number;
  riskyProcessSample: string[];
  maxLoad1: number;
  minFreeMemMb: number;
  maxRiskyProcesses: number;
  ok: boolean;
  reason: string | null;
};

export function parseProcessList(output: string, pattern = DEFAULT_RISKY_PATTERN): string[] {
  const re = new RegExp(pattern, "i");
  return output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => re.test(line))
    .filter((line) => !/process-pressure|pgrep|grep|awk|tailscaled be-child/i.test(line));
}

export function evaluatePressure(input: {
  checkedAt?: number;
  load1: number;
  freeMemMb: number;
  riskyProcesses: string[];
  maxLoad1?: number;
  minFreeMemMb?: number;
  maxRiskyProcesses?: number;
}): ProcessPressure {
  const maxLoad1 = input.maxLoad1 ?? 128;
  const minFreeMemMb = input.minFreeMemMb ?? 2048;
  const maxRiskyProcesses = input.maxRiskyProcesses ?? 6;
  const riskyProcessCount = input.riskyProcesses.length;
  let reason: string | null = null;

  if (input.freeMemMb < minFreeMemMb) {
    reason = `low_free_memory:${Math.round(input.freeMemMb)}MiB`;
  } else if (riskyProcessCount > maxRiskyProcesses) {
    reason = `too_many_risky_processes:${riskyProcessCount}`;
  } else if (input.load1 > maxLoad1) {
    reason = `high_load:${input.load1.toFixed(1)}`;
  }

  return {
    checkedAt: input.checkedAt ?? Date.now(),
    load1: input.load1,
    freeMemMb: input.freeMemMb,
    riskyProcessCount,
    riskyProcessSample: input.riskyProcesses.slice(0, 8),
    maxLoad1,
    minFreeMemMb,
    maxRiskyProcesses,
    ok: reason === null,
    reason,
  };
}

function readNumberEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

export async function readProcessPressure(): Promise<ProcessPressure> {
  let riskyProcesses: string[] = [];
  try {
    const { stdout } = await execFileAsync("ps", ["-eo", "pid=,pcpu=,pmem=,args="], {
      maxBuffer: 1024 * 1024,
      timeout: 5_000,
    });
    riskyProcesses = parseProcessList(stdout, process.env.SYMPHONY_PROCESS_GUARD_PATTERN ?? DEFAULT_RISKY_PATTERN);
  } catch {
    riskyProcesses = [];
  }

  return evaluatePressure({
    load1: loadavg()[0] ?? 0,
    freeMemMb: freemem() / 1024 / 1024,
    riskyProcesses,
    maxLoad1: readNumberEnv("SYMPHONY_PROCESS_GUARD_MAX_LOAD1", 128),
    minFreeMemMb: readNumberEnv("SYMPHONY_PROCESS_GUARD_MIN_FREE_MB", 2048),
    maxRiskyProcesses: readNumberEnv("SYMPHONY_PROCESS_GUARD_MAX_RISKY", 6),
  });
}
