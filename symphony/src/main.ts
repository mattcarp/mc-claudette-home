import { loadWorkflow } from "./workflow-loader.js";
import { Orchestrator } from "./orchestrator/orchestrator.js";
import { logger } from "./logger.js";
import { startHttpServer } from "./server.js";

async function main(): Promise<void> {
  const workflowPath = process.env.SYMPHONY_WORKFLOW ?? process.argv[2] ?? "./WORKFLOW.md";
  const portArg = process.argv.find((a) => a.startsWith("--port="));
  const cliPort = portArg ? Number(portArg.slice(7)) : null;

  const workflow = await loadWorkflow(workflowPath);
  logger.info("workflow.loaded", {
    path: workflow.sourcePath,
    tracker: workflow.config.tracker.kind,
    project_slug: workflow.config.tracker.project_slug,
    poll_ms: workflow.config.polling.interval_ms,
  });

  // Preflight validation already done by zod; ensure api key non-empty
  if (!workflow.config.tracker.api_key) {
    logger.error("preflight.missing_api_key");
    process.exit(1);
  }

  const orch = new Orchestrator(workflow, logger);
  await orch.start();

  const port = cliPort ?? workflow.config.server?.port;
  const host = workflow.config.server?.host ?? "127.0.0.1";
  let server: { close: () => void } | null = null;
  if (port) server = startHttpServer(orch, port, host, logger);

  const shutdown = async (signal: string) => {
    logger.info("shutdown", { signal });
    server?.close();
    await orch.stop();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch((err) => {
  logger.error("fatal", { err: err instanceof Error ? err.stack : String(err) });
  process.exit(1);
});
