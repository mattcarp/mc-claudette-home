import { createServer } from "node:http";
import type { Orchestrator } from "./orchestrator/orchestrator.js";
import type { Logger } from "./logger.js";

export function startHttpServer(
  orch: Orchestrator,
  port: number,
  host: string,
  log: Logger,
): { close: () => void } {
  const server = createServer((req, res) => {
    if (!req.url) {
      res.writeHead(400);
      res.end();
      return;
    }
    if (req.method === "GET" && req.url === "/api/v1/state") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(orch.snapshot(), null, 2));
      return;
    }
    if (req.method === "POST" && req.url === "/api/v1/refresh") {
      // Snapshot endpoint and a hint for the operator; tick is on its own timer
      res.writeHead(202, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ accepted: true }));
      return;
    }
    if (req.method === "GET" && req.url === "/") {
      const snap = orch.snapshot();
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(renderDashboard(snap));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  server.listen(port, host, () => {
    log.info("http.listening", { host, port });
  });
  return { close: () => server.close() };
}

function renderDashboard(snap: ReturnType<Orchestrator["snapshot"]>): string {
  const running = snap.running
    .map(
      (s) => `<tr>
        <td>${s.identifier}</td>
        <td>${s.phase}</td>
        <td>${s.turns}</td>
        <td>${(s.last_event_age_ms / 1000).toFixed(1)}s</td>
        <td>${s.totals.total}</td>
      </tr>`,
    )
    .join("");
  const retrying = snap.retrying
    .map(
      (r) => `<tr>
        <td>${r.identifier}</td>
        <td>${r.attempt}</td>
        <td>${(r.due_in_ms / 1000).toFixed(1)}s</td>
        <td><code>${escapeHtml(r.last_error)}</code></td>
      </tr>`,
    )
    .join("");
  return `<!doctype html>
<html><head><meta charset="utf-8"><title>Symphony</title>
<style>
  body { font: 14px/1.5 system-ui, -apple-system, sans-serif; margin: 2rem; max-width: 70rem; color: #111; }
  h1 { font-size: 1.2rem; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }
  th { font-weight: 500; color: #666; font-size: 0.85em; }
  code { font-size: 0.85em; color: #444; }
</style></head>
<body>
<h1>Symphony</h1>
<h2>Running (${snap.running.length})</h2>
<table><tr><th>Issue</th><th>Phase</th><th>Turns</th><th>Last event</th><th>Tokens</th></tr>${running || '<tr><td colspan="5" style="color:#888">none</td></tr>'}</table>
<h2>Retrying (${snap.retrying.length})</h2>
<table><tr><th>Issue</th><th>Attempt</th><th>Due</th><th>Last error</th></tr>${retrying || '<tr><td colspan="4" style="color:#888">none</td></tr>'}</table>
<h2>Totals</h2>
<pre>${escapeHtml(JSON.stringify(snap.totals, null, 2))}</pre>
</body></html>`;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;" : c === '"' ? "&quot;" : "&#39;",
  );
}
