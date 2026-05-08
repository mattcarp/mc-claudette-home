import type { Runner, RunOptions, AgentRunResult } from "./runner.js";

export class FailoverManager {
  private inFailover = false;
  private failoverAttempt = 0;
  private nextRetryAt = 0;

  constructor(
    private primaryRunner: Runner,
    private fallbackRunner: Runner,
  ) {}

  getState() {
    return {
      inFailover: this.inFailover,
      failoverAttempt: this.failoverAttempt,
      nextRetryAt: this.nextRetryAt
    };
  }

  async run(opts: RunOptions): Promise<AgentRunResult> {
    if (this.inFailover) {
      if (Date.now() >= this.nextRetryAt) {
        opts.log.info("failover.trying_primary_again", { attempt: this.failoverAttempt });
        const res = await this.primaryRunner(opts);
        
        if (!res.ok && res.reason === "rate_limit") {
          this.failoverAttempt++;
          // Exponential backoff: starts at 10 mins (600,000 ms), then 20 mins, 40 mins...
          // Maxes out at 24 hours
          const backoff = Math.min(10 * 60 * 1000 * Math.pow(2, this.failoverAttempt - 1), 24 * 60 * 60 * 1000);
          this.nextRetryAt = Date.now() + backoff;
          opts.log.info("failover.rate_limit_hit_again", { next_retry_in_ms: backoff });
          
          const fallbackRes = await this.fallbackRunner(opts);
          fallbackRes.usedFallback = true;
          return fallbackRes;
        }
        
        // Recovered successfully
        opts.log.info("failover.recovered", {});
        this.inFailover = false;
        this.failoverAttempt = 0;
        this.nextRetryAt = 0;
        return res;
      } else {
        // Still inside the failover backoff window
        opts.log.info("failover.using_fallback", { 
          time_remaining_ms: this.nextRetryAt - Date.now() 
        });
        const fallbackRes = await this.fallbackRunner(opts);
        fallbackRes.usedFallback = true;
        return fallbackRes;
      }
    }

    // Normal mode: try primary
    const res = process.env.FORCE_FAILOVER === "1" 
      ? { ok: false, reason: "rate_limit", sessionId: opts.resumeSessionId, usage: {}, events: [] } 
      : await this.primaryRunner(opts);
      
    if (!res.ok && res.reason === "rate_limit") {
      this.inFailover = true;
      this.failoverAttempt = 1;
      const backoff = 10 * 60 * 1000; // 10 minutes initially
      this.nextRetryAt = Date.now() + backoff;
      opts.log.info("failover.rate_limit_hit", { next_retry_in_ms: backoff });
      
      // Fall forward immediately to the fallback pipeline
      const fallbackRes = await this.fallbackRunner(opts);
      fallbackRes.usedFallback = true;
      return fallbackRes;
    }
    
    return res;
  }
}
