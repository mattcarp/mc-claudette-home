// Telegram notifier for state transitions in the Symphony harness.
//
// Mirrors the conventions of summary/symphony-summary.sh:
//   - HTML parse mode; only & < > need escaping in content
//   - TELEGRAM_BOT_TOKEN from Infisical (env)
//   - TELEGRAM_CHAT_ID from systemd unit env
//   - 10s curl-equivalent timeout, fire-and-forget at call sites
//
// Design notes:
//   - If either env var is missing, the notifier becomes a no-op. This keeps
//     local dev (no Telegram bot) from crashing the harness, and matches the
//     summary script's defensive ":${VAR:?...}" guards by failing softly here
//     instead of at startup — the harness itself doesn't need Telegram to run.
//   - We only fire on START and TERMINAL (Done / Canceled-via-marker / Failed-
//     with-retry-exhausted). Per-turn or per-retry pings would drown the chat;
//     the dashboard at :4754 already shows in-flight retries.
//   - All public methods catch their own errors. Telegram outages must never
//     take down a Symphony dispatch.
import type { Logger } from "../logger.js";
import type { Issue } from "../types.js";

const TELEGRAM_API = "https://api.telegram.org";
const REQUEST_TIMEOUT_MS = 10_000;

export interface SymphonyNotifier {
  started(issue: Issue): void;
  done(issue: Issue, info: { turns: number; tokens: number; usedFallback: boolean }): void;
  failed(issue: Issue, info: { attempt: number; lastError: string }): void;
}

const NOOP: SymphonyNotifier = {
  started: () => {},
  done: () => {},
  failed: () => {},
};

export function createTelegramNotifier(log: Logger): SymphonyNotifier {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) {
    log.info("notify.telegram_disabled", {
      reason: !token ? "missing TELEGRAM_BOT_TOKEN" : "missing TELEGRAM_CHAT_ID",
    });
    return NOOP;
  }
  log.info("notify.telegram_enabled", { chat_id_tail: chatId.slice(-4) });

  async function send(text: string): Promise<void> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch(`${TELEGRAM_API}/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          chat_id: chatId!,
          text,
          parse_mode: "HTML",
          disable_web_page_preview: "true",
        }),
        signal: ctrl.signal,
      });
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        log.warn("notify.telegram_http_failed", {
          status: resp.status,
          body: body.slice(0, 200),
        });
      }
    } catch (err) {
      log.warn("notify.telegram_send_error", { err: String(err) });
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    started(issue) {
      const text =
        `▶ <b>${esc(issue.identifier)}</b> started\n` +
        `· ${esc(issue.title)}` +
        (issue.url ? `\n<a href="${esc(issue.url)}">open in Linear</a>` : "");
      void send(text);
    },
    done(issue, info) {
      const fallbackMsg = info.usedFallback ? " ⚠️ (Completed via Fallback: DeepSeek/GPT-5.5)" : "";
      const text =
        `✓ <b>${esc(issue.identifier)}</b> done${fallbackMsg}\n` +
        `· ${esc(issue.title)}\n` +
        `· ${info.turns} turn${info.turns === 1 ? "" : "s"}, ${formatTokens(info.tokens)} tokens`;
      void send(text);
    },
    failed(issue, info) {
      const text =
        `⚠ <b>${esc(issue.identifier)}</b> failed (attempt ${info.attempt})\n` +
        `· ${esc(issue.title)}\n` +
        `· <code>${esc(truncate(info.lastError, 240))}</code>`;
      void send(text);
    },
  };
}

// HTML escape for Telegram parse_mode=HTML: only &, <, > need escaping in
// content per Telegram Bot API docs. (Tag attribute values have stricter rules
// but we control all attributes — only href on <a>, which we feed already-
// validated Linear URLs into.)
function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}
