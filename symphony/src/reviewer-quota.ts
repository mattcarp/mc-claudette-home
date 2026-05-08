/** Detect OpenAI / OpenRouter rate limits and exhaustion surfaced in reviewer errors. */

export function isReviewerQuotaOrRateLimit(reason: string): boolean {
  const r = reason.toLowerCase();
  return (
    r.includes("insufficient_quota") ||
    r.includes("rate_limit") ||
    r.includes("ratelimiterror") ||
    r.includes("error code: 429") ||
    r.includes(" 429") ||
    r.includes("status code 429") ||
    r.includes("too many requests") ||
    r.includes("resource exhausted")
  );
}

/** Milliseconds to pause new LangGraph dispatches after a reviewer quota hit (default 30m). */
export function reviewerQuotaCooldownMs(): number {
  const raw =
    process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS?.trim() ??
    process.env.SYMPHONY_REVIEWER_QUOTA_COOLDOWN_MS?.trim();
  if (!raw) return 30 * 60 * 1000;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : 30 * 60 * 1000;
}
