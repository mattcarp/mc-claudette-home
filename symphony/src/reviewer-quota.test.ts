import { describe, expect, it } from "vitest";
import { isReviewerQuotaOrRateLimit, reviewerQuotaCooldownMs } from "./reviewer-quota.js";

describe("isReviewerQuotaOrRateLimit", () => {
  it("returns true for OpenAI-style quota and 429 messages", () => {
    expect(
      isReviewerQuotaOrRateLimit(
        "LLM reviewer error (RateLimitError): Error code: 429 - { \"type\": \"insufficient_quota\" }",
      ),
    ).toBe(true);
    expect(isReviewerQuotaOrRateLimit("[LangGraph review_failed] MAG-1 | LLM reviewer error: 429")).toBe(true);
    expect(isReviewerQuotaOrRateLimit("rate_limit exceeded")).toBe(true);
    expect(isReviewerQuotaOrRateLimit("too many requests")).toBe(true);
  });

  it("returns false for normal review failures", () => {
    expect(isReviewerQuotaOrRateLimit("llm_review failed: diff is empty")).toBe(false);
    expect(isReviewerQuotaOrRateLimit("[LangGraph worker_tests_failed] MAG-2")).toBe(false);
  });
});

describe("reviewerQuotaCooldownMs", () => {
  it("defaults to 30 minutes", () => {
    const prev = process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS;
    const prev2 = process.env.SYMPHONY_REVIEWER_QUOTA_COOLDOWN_MS;
    delete process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS;
    delete process.env.SYMPHONY_REVIEWER_QUOTA_COOLDOWN_MS;
    try {
      expect(reviewerQuotaCooldownMs()).toBe(30 * 60 * 1000);
    } finally {
      if (prev === undefined) delete process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS;
      else process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS = prev;
      if (prev2 === undefined) delete process.env.SYMPHONY_REVIEWER_QUOTA_COOLDOWN_MS;
      else process.env.SYMPHONY_REVIEWER_QUOTA_COOLDOWN_MS = prev2;
    }
  });

  it("honors LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS", () => {
    const prev = process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS;
    process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS = "60000";
    try {
      expect(reviewerQuotaCooldownMs()).toBe(60_000);
    } finally {
      if (prev === undefined) delete process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS;
      else process.env.LANGGRAPH_REVIEWER_QUOTA_COOLDOWN_MS = prev;
    }
  });
});
