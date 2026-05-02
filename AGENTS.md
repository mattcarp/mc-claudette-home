# AGENTS.md — mc-claudette-home

Project context for AI coding agents (Claude Code, Cursor, Codex, Symphony workers).
This is the cross-tool standard file ([agents.md](https://agents.md/)). `CLAUDE.md` symlinks here.

## What this project is

**The Claudette product repo** — the home automation app of the Claudette family. Renamed from `mc-home` → `mc-claudette-home` on 2026-04-17 to align with sibling repos (`mc-claudette-voice`, etc.).

A natural-language home-control system where "Claudette" is the wake word, the brain, and (eventually) the brand. Built on open device protocols (Home Assistant, Zigbee, Matter) but with a proprietary AI layer that replaces rigid command grammar with conversational interaction.

**Pilot:** Xagħra, Gozo house — single Android POE touchscreen panel (YC-SM10P), then full deployment, then Valletta house, then product.

Read [`README.md`](README.md) and [`PRD.md`](PRD.md) before making non-trivial changes.

## Stack

- **Brain:** Python — `brain/` module: alert delivery, proactive alerts, whole-home audio routing. Tests via `pytest`.
- **Voice:** Python — `voice/` module: conversation fallback, intent parsing, HA bridge, panel readiness checks. Whisper STT (local) + Gemini Kore TTS for Claudette's voice.
- **Home Assistant integration:** Python + YAML — `ha/` module: setup script, automations, integrations, entity sync.
- **Dashboard:** Vanilla HTML/SVG — `dashboard/floor-plan.svg`, `floor-plan.html`, `index.html`. No build step.
- **Bridge:** `bridge/` — emerging (may be empty on some checkouts); reserved for cross-component glue when needed.
- **Hardware target:** Android POE touchscreen panels in each room. No cloud dependency for core function.
- **AI brain provider:** Claudette via OpenClaw.

## Hard rules (non-negotiable)

1. **No cloud dependency for core function.** Voice recognition, intent parsing, and device control must all work offline. Cloud-only paths are opt-in features, not the default.
2. **All secrets through Infisical.** API keys (Gemini, OpenAI, OpenClaw) come via `infisical run --env=dev -- python …`. Never plaintext `.env`.
3. **No production deploy without `[symphony:deploy-ok]` marker per-issue.** Production for this project is the Xagħra house. Don't push hardware-affecting changes (HA automations, panel firmware, audio routing) without explicit per-issue authorization.
4. **No mocks in tests.** Hit real Home Assistant via `ha_bridge`, real LLM providers, or fail honestly. The aphorism "if it can't fail honestly it isn't a test" applies.
5. **Don't push to `main` from agent runs.** Symphony branches stay local unless an issue carries `auto-push`.
6. **Don't introduce new dependencies without surfacing the choice.**
7. **Never commit changes unless the user explicitly asks.** No Co-Authored-By trailers; commits are authored by Matt Carpenter.
8. **Component scoping.** Issues should be scoped to ONE of `brain/`, `bridge/`, `dashboard/`, `ha/`, `voice/`. If an issue title doesn't name a component, assume it from the issue body. Cross-component changes need explicit acknowledgement in the commit body.
9. **The wake word "Claudette" and the personality across the family are load-bearing.** Voice / personality drift in `voice/` or `brain/` should be explicit, not incidental.

## Commands

```bash
infisical run --env=dev -- python3 -m pytest brain/             # run brain tests
infisical run --env=dev -- python3 -m pytest voice/             # voice tests (where they exist)
infisical run --env=dev -- python3 ha/sync_ha_entities.py       # sync entities from Home Assistant
infisical run --env=dev -- python3 brain/proactive_alerts.py    # run alerts daemon (example)
ha/setup_ha_docker.sh                                            # bring up local HA in Docker
```

For symphony harness work specifically (TS, lives in `symphony/`):

```bash
cd symphony && npm install && npx tsc --noEmit
```

## Layout

```
brain/                       Python — alert delivery, proactive alerts, whole-home audio
  alert_delivery.py
  proactive_alerts.py
  test_*.py                  pytest unit + integration tests
voice/                       Python — conversation, intent parsing, HA bridge
  conversation_fallback.py
  ha_bridge/                 typed HA event/command bridge
  ha_event_emitter.py
  intent_parser/             intent extraction
  panel_readiness.py
ha/                          Home Assistant glue
  automations/               YAML automations
  integrations/              custom integrations
  setup_ha_docker.sh
  sync_ha_entities.py
dashboard/                   Mission Control surface (vanilla HTML/SVG)
  floor-plan.svg
  floor-plan.html
  index.html
bridge/                      reserved cross-component glue (may be empty)
PRD.md                       Product requirements — read before architectural changes
README.md                    Vision + tagline
symphony/                    Linear→Claude harness (port 4750)
openspec/                    OpenSpec proposals + locked specs
```

## Conventions

- Python is the default language for brain, voice, and ha components.
- Use `python3` explicitly; do not assume `python` is on PATH.
- Tests live alongside the module (`brain/test_alert_delivery.py` next to `brain/alert_delivery.py`), not in a separate `tests/` tree.
- HA YAML automations live in `ha/automations/` and are version-controlled. Don't hand-edit Home Assistant's runtime config and forget to mirror.
- Dashboard is single-page, dependency-free. Keep it that way unless an issue justifies otherwise.
- Wake word: always "Claudette". Capitalisation matters in user-facing copy.

## Symphony / agent runs (this is important if you're invoked from Symphony)

Inside a fresh clone at `~/symphony_workspaces/<ISSUE-ID>/` on workshop, on branch `symphony/<ISSUE-ID>`. Read this file + `PRD.md` + the relevant component's source before making changes.

Component scoping: identify the target component from the issue. If unclear, write a `symphony-notes/<ISSUE-ID>.md` explaining the ambiguity and stop with `[symphony:done]`.

For Python changes: run `python3 -m pytest <component>/` if tests exist for the touched component. New modules need real tests (no mocks).

For TypeScript inside `symphony/`: `cd symphony && npx tsc --noEmit`.

## What NOT to do

- Don't deploy to the Xagħra panel without an explicit `[symphony:deploy-ok]` marker.
- Don't add features beyond what an issue/prompt asks for.
- Don't introduce a frontend framework, transpiler, or bundler in `dashboard/` without surfacing the choice.
- Don't write tests against mocks.
- Don't drift the wake word, the Claudette personality, or the cross-family naming.
- Don't use emojis in code or commits.
