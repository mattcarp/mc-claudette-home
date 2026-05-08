import { describe, expect, it } from "vitest";
import { evaluatePressure, parseProcessList } from "./process-pressure.js";

describe("process pressure guard", () => {
  it("detects risky process lines without counting its own shell plumbing", () => {
    const processes = parseProcessList(`
      123 10.0 2.0 node /home/sysop/.npm/_npx/abc/node_modules/.bin/task-master-ai
      124 20.0 4.0 /home/sysop/.local/bin/aider --model openai/gpt-5.5
      125  0.0 0.0 grep task-master-ai
      126  0.0 0.0 /usr/sbin/tailscaled be-child ssh --cmd pgrep -afi task-master-ai
    `);

    expect(processes).toHaveLength(2);
    expect(processes[0]).toContain("task-master-ai");
    expect(processes[1]).toContain("aider");
  });

  it("defers dispatch when process count exceeds the cap", () => {
    const pressure = evaluatePressure({
      checkedAt: 1,
      load1: 10,
      freeMemMb: 12_000,
      riskyProcesses: ["aider", "aider", "task-master-ai"],
      maxRiskyProcesses: 2,
    });

    expect(pressure.ok).toBe(false);
    expect(pressure.reason).toBe("too_many_risky_processes:3");
  });

  it("keeps dispatch open when resources are healthy", () => {
    const pressure = evaluatePressure({
      checkedAt: 1,
      load1: 4,
      freeMemMb: 8_000,
      riskyProcesses: ["aider"],
      maxRiskyProcesses: 2,
    });

    expect(pressure.ok).toBe(true);
    expect(pressure.reason).toBeNull();
  });
});
