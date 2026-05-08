import { runAiderTurn } from "./aider-runner.js";
import type { AgentRunResult, RunOptions, AgentEvent } from "./runner.js";
import { readFile, writeFile, unlink } from "node:fs/promises";
import { join } from "node:path";
import { execFile as execFileCallback } from "node:child_process";
import { promisify } from "node:util";
import { LinearClient } from "../tracker/linear.js";

const execFile = promisify(execFileCallback);

async function createDoneCommit(opts: RunOptions): Promise<void> {
  await execFile(
    "git",
    ["commit", "--allow-empty", "-m", `Finalize ${opts.session.identifier}`, "-m", "[symphony:done]"],
    { cwd: opts.cwd },
  );
}

async function postReviewComment(opts: RunOptions, round: number, feedback: string): Promise<void> {
  const body =
    feedback.trim() === "APPROVED"
      ? `GPT-5.5 (round ${round}): APPROVED`
      : `GPT-5.5 (round ${round}):\n\n${feedback}`;

  try {
    const linear = new LinearClient(opts.cfg.tracker);
    const result = await linear.addCommentToIssue(opts.session.issueId, body);
    if (!result.ok) {
      opts.log.warn("fallback.linear_comment_failed", { round, reason: result.reason });
    }
  } catch (err) {
    opts.log.warn("fallback.linear_comment_error", { round, err: String(err) });
  }
}

function refreshStaleSubturnClock(opts: RunOptions): void {
  const now = Date.now();
  if (opts.session.lastEventAt < now) {
    opts.session.lastEventAt = now;
  }
}

/**
 * Option A pipeline (Generator-Critic Loop):
 * 1. DeepSeek (generator) creates the code.
 * 2. GPT-5.5 (critic / evaluator) reviews the code and writes feedback.
 * 3. Loops back to DeepSeek if fixes are required.
 */
export async function runFallbackPipeline(opts: RunOptions): Promise<AgentRunResult> {
  opts.log.info("fallback.pipeline_start", {});

  let currentPrompt = opts.prompt;
  let totalUsage = { input_tokens: 0, output_tokens: 0, total_tokens: 0 };
  let allEvents: AgentEvent[] = [];
  let currentSessionId = opts.resumeSessionId;
  
  const maxRounds = 3;

  for (let round = 1; round <= maxRounds; round++) {
    opts.log.info("fallback.round_start", { round });
    await unlink(join(opts.cwd, "symphony-review.md")).catch(() => {});

    // Step 1: DeepSeek Generator
    refreshStaleSubturnClock(opts);
    const dsResult = await runAiderTurn({
      ...opts,
      prompt: currentPrompt,
      resumeSessionId: currentSessionId,
      cfg: {
        ...opts.cfg,
        agent: { ...opts.cfg.agent, model: "deepseek/deepseek-v4-pro" }
      },
      session: opts.session
    });
    
    if (dsResult.usage) {
      totalUsage.input_tokens += (dsResult.usage.input_tokens ?? 0);
      totalUsage.output_tokens += (dsResult.usage.output_tokens ?? 0);
      totalUsage.total_tokens += (dsResult.usage.total_tokens ?? 0);
    }
    allEvents.push(...dsResult.events);
    currentSessionId = dsResult.sessionId;

    if (!dsResult.ok) {
      opts.log.warn("fallback.generator_failed", { reason: dsResult.reason });
      return { ...dsResult, usage: totalUsage, events: allEvents };
    }
    
    // Step 2: GPT-5.5 Critic
    const reviewFile = join(opts.cwd, "symphony-review.md");
    
    let recentDiff = "";
    try {
      const { stdout } = await execFile("git", ["log", "-p", "-n", "3"], { cwd: opts.cwd, maxBuffer: 1024 * 1024 * 5 });
      recentDiff = stdout;
    } catch (err) {
      recentDiff = "Could not retrieve git diff.";
    }

    const gptPrompt = `Review the recent changes implemented for this task:

${opts.prompt}

Here are the most recent commits and their diffs:
\`\`\`diff
${recentDiff.slice(0, 20000)}
\`\`\`

Act as a strict Senior Engineer. Verify that the implementation is correct, there are no bugs, and our standards are followed. 
Write a file named \`symphony-review.md\`. 
- If the code is perfect and the task is fully complete, write EXACTLY the word "APPROVED" in the file. 
- If there are bugs, missing features, or improvements needed, write detailed feedback in the file for the junior developer to fix.
Do NOT attempt to fix the code yourself. Only write to \`symphony-review.md\` and commit it.`;

    // GPT-5.5 is a reasoning model and rejects Aider's default temperature=0
    // ("Only the default (1) value is supported"). Suppress temperature via
    // `--model-settings-file` (use_temperature: false) — see
    // symphony/aider-model-settings.yml.
    opts.log.info("fallback.evaluator_starting", { model: "gpt-5.5" });
    refreshStaleSubturnClock(opts);
    const gptResult = await runAiderTurn({
      ...opts,
      prompt: gptPrompt,
      resumeSessionId: currentSessionId,
      cfg: {
        ...opts.cfg,
        agent: { ...opts.cfg.agent, model: "openai/gpt-5.5" }
      },
      session: opts.session
    });
    
    if (gptResult.usage) {
      totalUsage.input_tokens += (gptResult.usage.input_tokens ?? 0);
      totalUsage.output_tokens += (gptResult.usage.output_tokens ?? 0);
      totalUsage.total_tokens += (gptResult.usage.total_tokens ?? 0);
    }
    allEvents.push(...gptResult.events);
    currentSessionId = gptResult.sessionId;

    if (!gptResult.ok) {
      opts.log.warn("fallback.evaluator_failed", { reason: gptResult.reason });
      return { ...gptResult, usage: totalUsage, events: allEvents };
    }

    // Check what the critic said
    let feedback = "";
    try {
      // Prefer the dedicated review file the model was instructed to write
      feedback = await readFile(reviewFile, "utf8");
    } catch {
      // Fall back to assistant text events if the file was not written
      const textEvents = gptResult.events.filter(e => e.kind === "assistant_text");
      if (textEvents.length > 0) {
        feedback = textEvents.map(e => (e as any).text).join("\n");
      }
    }

    opts.log.info("fallback.evaluator_feedback", { feedback: feedback.slice(0, 200) });

    // No output from the reviewer means a silent failure — do not mislead Linear
    // by posting a "no feedback" comment; treat it as a hard failure instead.
    if (!feedback.trim()) {
      opts.log.warn("fallback.evaluator_no_output", { round });
      return {
        ok: false,
        reason: "fallback_reviewer_no_output",
        sessionId: currentSessionId,
        usage: totalUsage,
        events: allEvents,
      };
    }

    if (feedback.trim() === "APPROVED") {
      opts.log.info("fallback.evaluator_approved", {});
      // Commit first; only post approval to Linear after the commit succeeds so
      // Linear never shows an approval that has no corresponding done marker.
      await createDoneCommit(opts);
      await postReviewComment(opts, round, feedback);
      return {
        ok: true,
        reason: undefined,
        sessionId: currentSessionId,
        usage: totalUsage,
        events: allEvents,
      };
    } else {
      opts.log.info("fallback.evaluator_requested_changes", { round });
      // Post the requested-changes feedback to Linear so the pipeline is visible
      await postReviewComment(opts, round, feedback);
      currentPrompt = `The Senior Engineer reviewed your previous work and provided the following feedback. Please implement the required fixes:

${feedback}

Make real code changes and commit them. Do not only edit symphony-review.md. Do not create [symphony:done] yet.`;

      allEvents.push({ kind: "assistant_text" as const, text: `GPT-5.5 Feedback: ${feedback}` });
    }
  }

  return {
    ok: false,
    reason: `fallback_review_not_approved_after_${maxRounds}_rounds`,
    sessionId: currentSessionId,
    usage: totalUsage,
    events: allEvents
  };
}
