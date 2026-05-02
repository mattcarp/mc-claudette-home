---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  # Hardcoded for mc-claudette-home so a single Infisical workspace
  # (shared with mc-briefings, mc-siam, mc-claudette-voice) doesn't collide
  # on the LINEAR_TEAM_KEY env var.
  team_key: CLH
  active_states: [Todo, "In Progress"]
  terminal_states: [Done, Canceled, Cancelled, Duplicate]
  exclude_labels: [tutorial, draft, manual-only, no-symphony]

polling:
  interval_ms: 30000

workspace:
  root: ~/symphony_workspaces

hooks:
  # IMPORTANT: workshop's Infisical workspace injects a stale `GITHUB_TOKEN`
  # (a `gho_…` value used by another consumer). When set, `gh auth git-credential`
  # returns the env-var token to git, overriding gh's stored credential and
  # failing auth with "Invalid username or token". `unset GITHUB_TOKEN` lets
  # gh's stored credential in ~/.config/gh/hosts.yml win.
  after_create: |
    set -euo pipefail
    unset GITHUB_TOKEN
    git clone https://github.com/mattcarp/mc-claudette-home.git .
    git checkout -b symphony/$SYMPHONY_ISSUE_IDENTIFIER

  # Self-heals an empty workspace (after_create may have been skipped on a
  # retry where the dir already exists). Same GITHUB_TOKEN unset rationale.
  before_run: |
    set -euo pipefail
    unset GITHUB_TOKEN
    if [ -z "$(ls -A . 2>/dev/null)" ]; then
      git clone https://github.com/mattcarp/mc-claudette-home.git .
      git checkout -b symphony/$SYMPHONY_ISSUE_IDENTIFIER 2>/dev/null || git checkout symphony/$SYMPHONY_ISSUE_IDENTIFIER
    fi
    git fetch origin main --quiet || true

  timeout_ms: 120000

agent:
  max_concurrent_agents: 2
  max_turns: 6
  max_retry_backoff_ms: 600000

codex:
  command: >-
    claude -p
    --output-format=stream-json
    --verbose
    --permission-mode=acceptEdits
    --allowedTools "Read,Edit,Write,Glob,Grep,Bash(git status),Bash(git diff:*),Bash(git log:*),Bash(git add:*),Bash(git commit:*),Bash(git checkout:*),Bash(git push:*),Bash(python:*),Bash(python3:*),Bash(pytest:*),Bash(uv:*),Bash(pip:*),Bash(npx tsc:*),Bash(npm install:*),Bash(npm run:*)"
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000

server:
  port: 4750
  host: 127.0.0.1
---

# Task

You are working on Linear issue **{{ issue.identifier }}**: {{ issue.title }}

{% if issue.description %}
## Description

{{ issue.description }}
{% endif %}

{% if issue.labels.size > 0 %}
**Labels:** {{ issue.labels | join: ", " }}
{% endif %}

{% if attempt %}
> **Continuation (attempt {{ attempt }}).** A previous turn ran. Pick up where it left off and take the next sensible step toward closing the issue.
{% endif %}

## Working agreement

- You are inside a fresh git worktree of `mc-claudette-home`. The branch `symphony/{{ issue.identifier }}` is checked out.
- Read [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) and `PRD.md` before making changes.
- The repo is multi-component: `brain/`, `bridge/`, `dashboard/`, `ha/`, `voice/`. Issues should be scoped to one component unless they explicitly span the boundary; if an issue isn't scoped, assume the component named in the title.
- Make focused, minimal changes that map directly to the issue.
- For Python changes: run `python3 -m pytest <component>/` if tests exist for the touched component. If you add a new module, add at least one real test alongside it (no mocks; hit the real interface or skip honestly).
- For dashboard HTML changes: render the page locally if you can; otherwise document the verification step in the commit body.
- Commit with a message that references `{{ issue.identifier }}`. Do not push unless the issue's labels include `auto-push`.

## Closing the issue (REQUIRED)

When you have completed the work for `{{ issue.identifier }}`, include the marker `[symphony:done]` in your **final commit's message body**. Without the marker, the harness will dispatch additional turns until `max_turns` is reached.

If the issue is ambiguous and you cannot proceed, write a short note at `symphony-notes/{{ issue.identifier }}.md` explaining what you'd need to know, commit it with `[symphony:done]` in the message, and stop.

## Issue link

{{ issue.url }}
