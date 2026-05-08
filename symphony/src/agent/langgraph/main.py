import json
import os
import argparse
import subprocess
import sys
import traceback
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END, START, MessagesState
from langgraph.types import Command
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from events import emit
from linear_client import LinearGraphQLClient
from model_router import model_route
from planner import classify_issue, make_parallel_batches
from state import LinearIssue
from supervisor import execute_batch, execute_one_plan
from tools import run_allowed

DONE_MARKER = "[symphony:done]"
AUTO_DONE_TRUE = {"1", "true", "yes", "on"}


@tool
def git_status() -> str:
    """Show concise git status for the current workspace."""
    emit("tool_use", tool="git_status")
    return run_allowed(["git", "status", "--short"], Path.cwd())


@tool
def git_diff() -> str:
    """Show the current workspace diff."""
    emit("tool_use", tool="git_diff")
    return run_allowed(["git", "diff"], Path.cwd())


tools = [git_status, git_diff]
tool_node = ToolNode(tools)


def get_llm(model_name: str):
    return ChatOpenAI(model=model_name, temperature=0)


def supervisor_node(state: MessagesState):
    emit("node_started", node="Supervisor")
    messages = state["messages"]
    last_msg = messages[-1]

    if isinstance(last_msg, HumanMessage):
        return Command(goto="coder")

    content = getattr(last_msg, "content", "").lower()
    if "[symphony:done]" in content or "approved" in content:
        return Command(goto=END)

    return Command(goto="coder")


def coder_node(state: MessagesState):
    emit("node_started", node="Coder")
    route = model_route()
    llm = get_llm(route.worker)
    llm_with_tools = llm.bind_tools(tools)

    messages = state["messages"]
    system_msg = {
        "role": "system",
        "content": (
            "You are a Coder. Implement the requested feature. You may inspect git "
            "status and diff through narrow tools only. When finished, say you are ready for review."
        ),
    }
    response = llm_with_tools.invoke([system_msg] + list(messages))

    if response.content:
        emit("assistant_text", text="Coder: " + response.content)

    if hasattr(response, "response_metadata") and "token_usage" in response.response_metadata:
        usage = response.response_metadata["token_usage"]
        emit(
            "usage",
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        )

    if hasattr(response, "tool_calls") and response.tool_calls:
        return Command(update={"messages": [response]}, goto="tools")

    return Command(update={"messages": [response]}, goto="reviewer")


def reviewer_node(state: MessagesState):
    emit("node_started", node="Reviewer")
    route = model_route()
    llm = get_llm(route.reviewer)

    system_msg = {
        "role": "system",
        "content": (
            "You are a Reviewer. Inspect the code changes made. If the code is correct "
            "and fully implements the request, output '[symphony:done] Approved'. "
            "Otherwise, explain what needs fixing."
        ),
    }
    response = llm.invoke([system_msg] + list(state["messages"]))

    if response.content:
        emit("assistant_text", text="Reviewer: " + response.content)

    if hasattr(response, "response_metadata") and "token_usage" in response.response_metadata:
        usage = response.response_metadata["token_usage"]
        emit(
            "usage",
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        )
    return Command(update={"messages": [response]}, goto="supervisor")


def build_graph():
    workflow = StateGraph(MessagesState)

    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("coder", coder_node)
    workflow.add_node("reviewer", reviewer_node)
    workflow.add_node("tools", tool_node)

    workflow.add_edge(START, "supervisor")
    workflow.add_edge("tools", "coder")

    return workflow.compile()


def load_fixture_issues(path: str) -> list[LinearIssue]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [
        LinearIssue(
            id=item["id"],
            identifier=item["identifier"],
            title=item["title"],
            description=item.get("description", ""),
            state=item.get("state", "Todo"),
            labels=tuple(item.get("labels", [])),
            priority=item.get("priority"),
            blocked_by=tuple(item.get("blocked_by", [])),
        )
        for item in raw
    ]


def emit_plan(issues: list[LinearIssue], max_workers: int):
    plans = [classify_issue(issue) for issue in issues]
    for plan in plans:
        emit(
            "issue_classified",
            identifier=plan.issue.identifier,
            classification=plan.classification,
            reason=plan.reason,
            predicted_paths=list(plan.predicted_paths),
            risk=plan.risk,
        )
    batches = make_parallel_batches(plans, max_workers=max_workers)
    for batch in batches:
        emit(
            "parallel_batch_planned",
            batch_id=batch.batch_id,
            identifiers=[plan.issue.identifier for plan in batch.plans],
        )
    return plans, batches


def maybe_comment_needs_human(
    args: argparse.Namespace,
    issues: list[LinearIssue],
    plans,
    client: LinearGraphQLClient | None = None,
) -> None:
    if not args.plan_linear_team:
        return
    if args.execute_one or args.execute_batch:
        return
    needs_comment = [p for p in plans if p.classification in {"needs_human", "blocked", "too_large"}]
    if not needs_comment:
        return
    if not args.linear_comment_apply:
        emit("linear_comment_dry_run", identifiers=[p.issue.identifier for p in needs_comment])
        return

    client = client or LinearGraphQLClient()
    issue_by_identifier = {issue.identifier: issue for issue in issues}
    for plan in needs_comment:
        issue = issue_by_identifier[plan.issue.identifier]
        body = (
            "LangGraph supervisor result: needs human\n\n"
            f"Reason: {plan.reason}\n"
            "Next action: run serially, unblock dependencies, or split the issue."
        )
        client.add_comment(issue.id, body)
        emit("linear_comment_created", identifier=issue.identifier)


def langgraph_auto_done_enabled() -> bool:
    return os.environ.get("LANGGRAPH_AUTO_DONE", "").strip().lower() in AUTO_DONE_TRUE


def langgraph_auto_merge_enabled() -> bool:
    return os.environ.get("LANGGRAPH_AUTO_MERGE", "").strip().lower() in AUTO_DONE_TRUE


def issue_allows_auto_push(plan) -> bool:
    return any(label.strip().lower() == "auto-push" for label in plan.issue.labels)


def auto_merge_worktree(plan, worktree: Path) -> tuple[bool, str]:
    """Push the worker's branch, open a PR, squash-merge, return PR url or err.

    Side effects:
      - git push origin <branch>:<branch>
      - gh pr create --base main --head <branch>
      - gh pr merge <pr> --squash --delete-branch

    Returns (True, pr_url) on success or (False, "<stage>: <stderr>") on failure.
    """
    issue_id = plan.issue.identifier

    branch_proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree, text=True, capture_output=True,
    )
    branch = branch_proc.stdout.strip()
    if branch_proc.returncode != 0 or not branch:
        return False, f"branch lookup failed: {branch_proc.stderr.strip()[:200]}"

    title_proc = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=worktree, text=True, capture_output=True,
    )
    title = title_proc.stdout.strip() or f"{issue_id}: autonomous patch"

    push_proc = subprocess.run(
        ["git", "push", "origin", f"{branch}:{branch}"],
        cwd=worktree, text=True, capture_output=True,
    )
    if push_proc.returncode != 0:
        return False, f"push failed: {push_proc.stderr.strip()[:300]}"

    pr_body = (
        f"Closes {issue_id}.\n\n"
        f"Autonomous patch from LangGraph supervisor:\n"
        f"- Worker: {os.environ.get('LANGGRAPH_WORKER_MODEL', 'deepseek/deepseek-v4-pro')}\n"
        f"- Reviewer: {os.environ.get('LANGGRAPH_REVIEWER_MODEL', 'openai/gpt-5.5')}\n"
        f"- Deterministic + worker-tests + LLM review gates passed.\n"
        f"- Worktree on workshop: {worktree}\n"
    )
    pr_create = subprocess.run(
        ["gh", "pr", "create", "--base", "main", "--head", branch,
         "--title", f"{issue_id}: {title}", "--body", pr_body],
        cwd=worktree, text=True, capture_output=True,
    )
    if pr_create.returncode != 0:
        return False, f"pr create failed: {pr_create.stderr.strip()[:300]}"
    pr_url = pr_create.stdout.strip()

    merge_proc = subprocess.run(
        ["gh", "pr", "merge", pr_url, "--squash"],
        cwd=worktree, text=True, capture_output=True,
    )
    if merge_proc.returncode != 0:
        return False, f"pr merge failed: {merge_proc.stderr.strip()[:300]}"

    return True, pr_url


def try_transition_issue(
    client: LinearGraphQLClient,
    team_key: str,
    issue: LinearIssue,
    state_name: str,
) -> None:
    try:
        updated = client.transition_issue_to_state(team_key, issue.id, state_name)
    except Exception as exc:
        emit("linear_state_update_failed", identifier=issue.identifier, state=state_name, reason=str(exc))
        return
    if updated:
        emit("linear_state_updated", identifier=issue.identifier, state=state_name)
    else:
        emit("linear_state_update_skipped", identifier=issue.identifier, state=state_name, reason="state not found")


def try_add_comment(client: LinearGraphQLClient, issue: LinearIssue, body: str) -> None:
    try:
        client.add_comment(issue.id, body)
    except Exception as exc:
        emit("linear_comment_failed", identifier=issue.identifier, reason=str(exc))
        return
    emit("linear_comment_created", identifier=issue.identifier)


def linear_execution_hooks(client: LinearGraphQLClient, team_key: str):
    def on_worker_started(plan, _worktree: Path) -> None:
        if plan.issue.state == "Todo":
            try_transition_issue(client, team_key, plan.issue, "In Progress")

    def on_worker_failed(plan, worktree: Path, result) -> None:
        body = (
            "LangGraph worker failed.\n\n"
            f"Reason: {result.reason}\n"
            f"Worktree: {worktree}\n\n"
            "Next action: inspect the worktree and retry after fixing the worker failure."
        )
        try_add_comment(client, plan.issue, body)

    def on_review_passed(plan, worktree: Path, result) -> None:
        auto_done = langgraph_auto_done_enabled()
        auto_merge_requested = langgraph_auto_merge_enabled()
        auto_merge_allowed = auto_merge_requested and issue_allows_auto_push(plan)
        merge_url: str | None = None
        merge_error: str | None = None
        if auto_merge_allowed:
            ok, msg = auto_merge_worktree(plan, worktree)
            if ok:
                merge_url = msg
                emit("auto_merge_succeeded", identifier=plan.issue.identifier, pr_url=msg)
            else:
                merge_error = msg
                emit("auto_merge_failed", identifier=plan.issue.identifier, reason=msg)
        elif auto_merge_requested:
            emit(
                "auto_merge_skipped",
                identifier=plan.issue.identifier,
                reason="missing auto-push label",
            )

        body_lines = [
            "LangGraph worker completed and deterministic + tests + LLM gates passed.",
            "",
            f"Worktree: {worktree}",
        ]
        if merge_url:
            body_lines.append(f"Merged to main: {merge_url}")
        elif merge_error:
            body_lines.append(f"Auto-merge failed (left as branch): {merge_error}")
        elif auto_merge_requested:
            body_lines.append("Auto-merge skipped: issue is missing the `auto-push` label.")
        else:
            body_lines.append("Status: ready for human review.")

        if auto_done:
            try_transition_issue(client, team_key, plan.issue, "Done")
            body_lines.append("")
            body_lines.append("Done transition requested by LANGGRAPH_AUTO_DONE.")
        elif not merge_url:
            if plan.issue.state != "In Review":
                try_transition_issue(client, team_key, plan.issue, "In Review")
            body_lines.append("")
            body_lines.append("Moved to In Review for human attention.")
            body_lines.append(f"Worker {DONE_MARKER} markers are review signals only.")
        try_add_comment(client, plan.issue, "\n".join(body_lines))

    def on_classification_blocked(plan, reason: str) -> None:
        # Move classification-blocked tickets out of active states so the
        # orchestrator stops re-dispatching them every retry cycle. If the
        # ticket is already In Review (a human picked it up) we stay quiet to
        # avoid noise.
        if plan.issue.state == "In Review":
            return
        try_transition_issue(client, team_key, plan.issue, "In Review")
        body = (
            "LangGraph supervisor declined to execute this ticket autonomously.\n\n"
            f"Classification: {plan.classification} (risk: {plan.risk})\n"
            f"Reason: {plan.reason}\n"
            f"Block: {reason}\n\n"
            "Moved to In Review for human attention. To re-queue for autonomous "
            "work, narrow the scope or split into smaller actions and move back to Todo."
        )
        try_add_comment(client, plan.issue, body)

    return on_worker_started, on_worker_failed, on_review_passed, on_classification_blocked


def repo_root_for(cwd: Path) -> Path:
    if cwd.name == "symphony":
        return cwd.parent
    return cwd


def run_prompt_graph(args: argparse.Namespace) -> None:
    if not args.prompt_file or not args.cwd:
        raise RuntimeError("--prompt-file and --cwd are required when not running planner mode")
    os.chdir(args.cwd)
    if args.model:
        os.environ["LANGGRAPH_MODEL"] = args.model
    with open(args.prompt_file, "r", encoding="utf-8") as f:
        prompt_text = f.read()

    graph = build_graph()
    initial_state = {"messages": [HumanMessage(content=prompt_text)]}
    for _ in graph.stream(initial_state, {"recursion_limit": 50}, stream_mode="updates"):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file")
    parser.add_argument("--cwd")
    parser.add_argument("--model")
    parser.add_argument("--planner-fixture")
    parser.add_argument("--plan-linear-team")
    parser.add_argument("--states", default="Todo,In Progress")
    parser.add_argument("--first", type=int, default=50)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--execute-one", action="store_true")
    parser.add_argument("--execute-batch", action="store_true")
    parser.add_argument("--issue-id")
    parser.add_argument("--worker-worktree-root", default=".langgraph-worktrees")
    parser.add_argument("--linear-comment-apply", action="store_true")
    parser.add_argument("--allow-serial", action="store_true")
    return parser


def result_exit_code(results) -> int:
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.model:
        os.environ["LANGGRAPH_MODEL"] = args.model

    try:
        route = model_route()
        emit("model_route_selected", planner=route.planner, worker=route.worker, reviewer=route.reviewer)

        issues: list[LinearIssue] | None = None
        linear_client: LinearGraphQLClient | None = None
        if args.planner_fixture:
            issues = load_fixture_issues(args.planner_fixture)
        elif args.plan_linear_team:
            states = [state.strip() for state in args.states.split(",") if state.strip()]
            linear_client = LinearGraphQLClient()
            issues = linear_client.fetch_active_issues(args.plan_linear_team, states, args.first)

        if issues is not None:
            plans, batches = emit_plan(issues, args.max_workers)
            maybe_comment_needs_human(args, issues, plans)

            cwd = Path.cwd()
            repo_root = repo_root_for(cwd)
            worker_root = repo_root / args.worker_worktree_root

            if args.execute_one:
                if not args.issue_id:
                    raise RuntimeError("--execute-one requires --issue-id")
                matching = [plan for plan in plans if plan.issue.identifier == args.issue_id]
                if not matching:
                    raise RuntimeError(f"issue not found in planner input: {args.issue_id}")
                hooks: tuple = (None, None, None, None)
                if linear_client and args.plan_linear_team:
                    hooks = linear_execution_hooks(linear_client, args.plan_linear_team)
                result = execute_one_plan(
                    matching[0],
                    repo_root,
                    worker_root,
                    on_worker_started=hooks[0],
                    on_worker_failed=hooks[1],
                    on_review_passed=hooks[2],
                    on_classification_blocked=hooks[3],
                    allow_serial=args.allow_serial,
                )
                sys.exit(result_exit_code([result]))
            elif args.execute_batch:
                if not batches:
                    emit("parallel_batch_completed", batch_id="batch-0", passed=[], failed=[])
                else:
                    hooks = (None, None, None, None)
                    if linear_client and args.plan_linear_team:
                        hooks = linear_execution_hooks(linear_client, args.plan_linear_team)
                    results = execute_batch(
                        batches[0],
                        repo_root,
                        worker_root,
                        args.max_workers,
                        on_worker_started=hooks[0],
                        on_worker_failed=hooks[1],
                        on_review_passed=hooks[2],
                    )
                    sys.exit(result_exit_code(results))
        else:
            run_prompt_graph(args)

    except Exception:
        emit("error", error=traceback.format_exc())
        sys.exit(1)
