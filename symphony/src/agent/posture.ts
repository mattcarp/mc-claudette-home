// Shared posture-enforcement helpers. The audit doc 2026-05-04 found that
// `Bash(git push:*)` was sitting in every harness's allowlist, with the
// "no auto-push" rule enforced only in the prompt body. This module is
// the runtime guardrail.
//
// Layered defense:
//   1. stripPushFromAllowlist  — removes Bash(git push:*) variants from a
//                                 command string before spawn (claude-code).
//   2. assertNoPushSurvives    — post-strip invariant. If anything matching
//                                 git push remains, refuse to spawn.
//   3. enforceProductionPosture — strip + assert + structured log.
//   4. auditAllowlistForPosture — load-time warning so operators see the
//                                 problem at config layer, not just runtime.
//   5. refusingPrePushHook     — bash hook for runners with no allowlist
//                                 surface (Aider). Floor-level guarantee.
//
// PostureLogger is intentionally a structural subset of Logger from
// ../logger.ts — keeps this module dependency-free so it's easy to test
// and easy to call from anywhere.

const PUSH_PATTERNS: ReadonlyArray<RegExp> = [
  /Bash\(git push[^)]*\)\s*,?\s*/gi,
  /Bash\(git push\)\s*,?\s*/gi,
];

export interface StripResult {
  stripped: string;
  removed: string[];
}

export function stripPushFromAllowlist(cmd: string): StripResult {
  const removed: string[] = [];
  let stripped = cmd;
  for (const re of PUSH_PATTERNS) {
    stripped = stripped.replace(re, (m) => {
      removed.push(m.trim().replace(/,$/, ""));
      return "";
    });
  }
  // Tidy up double commas / dangling separators left behind. Be conservative
  // — don't break quoting or tokens that legitimately contain commas.
  stripped = stripped.replace(/,\s*,/g, ",").replace(/"\s*,\s*"/g, '","');
  return { stripped, removed };
}

export function assertNoPushSurvives(cmd: string): void {
  if (/Bash\([^)]*\bgit\s+push\b[^)]*\)/i.test(cmd)) {
    throw new Error(
      "posture=production but allowlist still grants git push. " +
        "stripPushFromAllowlist did not catch it; refusing to spawn."
    );
  }
}

export interface PostureLogger {
  info: (event: string, fields: Record<string, unknown>) => void;
  warn: (event: string, fields: Record<string, unknown>) => void;
}

export function enforceProductionPosture(cmd: string, log: PostureLogger): string {
  const { stripped, removed } = stripPushFromAllowlist(cmd);
  if (removed.length > 0) {
    log.warn("posture.allowlist_stripped", {
      posture: "production",
      removed,
      hint: "production WORKFLOW.md should omit Bash(git push:*) at source",
    });
  } else {
    log.info("posture.allowlist_ok", {
      posture: "production",
      reason: "no push capability found in allowlist",
    });
  }
  assertNoPushSurvives(stripped);
  return stripped;
}

export function auditAllowlistForPosture(
  cmd: string,
  posture: "greenfield" | "production",
): string[] {
  const warnings: string[] = [];
  if (posture !== "production") return warnings;
  const { removed } = stripPushFromAllowlist(cmd);
  if (removed.length > 0) {
    warnings.push(
      `posture=production but WORKFLOW.md allowlist contains push capability(ies): ` +
        `${removed.join(", ")}. Runtime will strip these, but the WORKFLOW.md should ` +
        `be edited so the policy is visible at the config layer too.`,
    );
  }
  return warnings;
}

export function refusingPrePushHook(): string {
  return [
    "#!/usr/bin/env bash",
    "# Installed by Symphony for posture=production workspaces.",
    "# Runners without an allowlist surface (Aider, LangGraph tools.py)",
    "# cannot otherwise be prevented from pushing. This hook is the floor.",
    "echo 'Symphony posture=production: git push is blocked at hook level.' >&2",
    "echo 'Set posture: greenfield in WORKFLOW.md if you intend to push.' >&2",
    "exit 1",
    "",
  ].join("\n");
}
