import { describe, it, expect } from "vitest";
import {
  stripPushFromAllowlist,
  assertNoPushSurvives,
  auditAllowlistForPosture,
  enforceProductionPosture,
  refusingPrePushHook,
} from "./posture.js";

describe("stripPushFromAllowlist", () => {
  it("removes Bash(git push:*) and trailing comma", () => {
    const cmd = `claude -p --allowedTools "Read,Bash(git push:*),Edit"`;
    const { stripped, removed } = stripPushFromAllowlist(cmd);
    expect(removed.length).toBe(1);
    expect(removed[0]).toMatch(/Bash\(git push:\*\)/);
    expect(stripped).not.toMatch(/git push/i);
    expect(stripped).toMatch(/Read/);
    expect(stripped).toMatch(/Edit/);
  });

  it("catches Bash(git push -f:*) variants too", () => {
    const cmd = `claude --allowedTools "Bash(git push -f:*),Read"`;
    const { removed } = stripPushFromAllowlist(cmd);
    expect(removed.length).toBe(1);
  });

  it("catches bare Bash(git push) with no glob", () => {
    const cmd = `claude --allowedTools "Bash(git push),Read"`;
    const { stripped, removed } = stripPushFromAllowlist(cmd);
    expect(removed.length).toBe(1);
    expect(stripped).not.toMatch(/git push/i);
  });

  it("is idempotent", () => {
    const cmd = `--allowedTools "Read,Bash(git push:*),Edit"`;
    const a = stripPushFromAllowlist(cmd).stripped;
    const b = stripPushFromAllowlist(a).stripped;
    expect(a).toBe(b);
  });

  it("is a no-op on a clean command", () => {
    const cmd = `--allowedTools "Read,Edit,Bash(git status)"`;
    const { stripped, removed } = stripPushFromAllowlist(cmd);
    expect(removed.length).toBe(0);
    expect(stripped).toBe(cmd);
  });
});

describe("assertNoPushSurvives", () => {
  it("throws when git push survives", () => {
    expect(() => assertNoPushSurvives(`--allowedTools "Bash(git push:*)"`)).toThrow();
  });
  it("throws on weird whitespace variants", () => {
    expect(() => assertNoPushSurvives(`--allowedTools "Bash(git  push  -f)"`)).toThrow();
  });
  it("is silent on a clean command", () => {
    expect(() => assertNoPushSurvives(`--allowedTools "Bash(git status)"`)).not.toThrow();
  });
});

describe("auditAllowlistForPosture", () => {
  it("warns when production keeps push", () => {
    const warns = auditAllowlistForPosture(`--allowedTools "Bash(git push:*)"`, "production");
    expect(warns.length).toBe(1);
    expect(warns[0]).toMatch(/production/);
  });
  it("is silent on greenfield", () => {
    const warns = auditAllowlistForPosture(`--allowedTools "Bash(git push:*)"`, "greenfield");
    expect(warns.length).toBe(0);
  });
  it("is silent on a clean production allowlist", () => {
    const warns = auditAllowlistForPosture(`--allowedTools "Read,Edit"`, "production");
    expect(warns.length).toBe(0);
  });
});

describe("enforceProductionPosture", () => {
  it("logs and strips when push is present", () => {
    const calls: Array<{ event: string }> = [];
    const log = {
      info: (e: string) => calls.push({ event: e }),
      warn: (e: string) => calls.push({ event: e }),
    };
    const out = enforceProductionPosture(
      `--allowedTools "Read,Bash(git push:*),Edit"`,
      log,
    );
    expect(out).not.toMatch(/git push/i);
    expect(calls.some((c) => c.event === "posture.allowlist_stripped")).toBe(true);
  });

  it("logs ok when allowlist is already clean", () => {
    const calls: Array<{ event: string }> = [];
    const log = {
      info: (e: string) => calls.push({ event: e }),
      warn: (e: string) => calls.push({ event: e }),
    };
    enforceProductionPosture(`--allowedTools "Read,Edit"`, log);
    expect(calls.some((c) => c.event === "posture.allowlist_ok")).toBe(true);
  });
});

describe("refusingPrePushHook", () => {
  it("is bash and exits non-zero", () => {
    const hook = refusingPrePushHook();
    expect(hook).toMatch(/^#!\/usr\/bin\/env bash/);
    expect(hook).toMatch(/exit 1/);
  });
});
