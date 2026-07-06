# Presence Constraints — design note

**Date:** 2026-05-08
**Status:** Open question / design vocabulary
**Source:** Conversation with Claudia (Opus 4.7)

## TL;DR

A **presence constraint** is an unstated requirement that some object, person, or
state be physically *here* for an action to make sense. Humans assume them
silently — "wash the car" implies the car is with you. LLMs do not. They treat
the salient surface number (distance, price, count) as the dominant signal and
override the implicit constraint.

For Claudette Home, presence constraints are not a curiosity — they are the
core of the product. A voice assistant living in a kitchen has to know that
the kettle is in the kitchen, the kid is in bed, the oven is preheating, the
front door just closed. Every useful utterance is a presence constraint
chained to an action.

## The car-wash failure mode

Prompt: *"The car wash is 40 m from my house. Should I walk or drive to wash my
car?"*

Most current LLMs answer "walk" — short distance, healthy, environmentally
friendly, etc. They never notice that arriving without the car defeats the
goal.

In Itamar Golan's Feb 2026 single-shot test:
- **Passed:** GPT-5.2 Thinking, Opus 4.6, Gemini 3 Pro
- **Failed:** GPT-5.2 Instant, GPT-4o, Haiku 4.5, Sonnet 4.5, Gemini 3 Fast,
  Grok 4.1 (all variants)

So even within one model family, smaller/faster siblings face-plant. Relevant
for us — Claudette Home will lean on small local models for latency.

## The CMU paper

Li, Zhang, Jiang, Krishnan, Padman (2026).
*The Model Says Walk: How Surface Heuristics Override Implicit Constraints in
LLM Reasoning.*
https://arxiv.org/html/2603.29025

Key findings worth burning into our priors:

1. **Surface cue dominance.** The distance cue is 8.7×–38× more influential
   than the actual goal. The model is reasoning about the number, not the task.
2. **Heuristic Override Benchmark (HOB).** 500 minimal-pair instances, 14
   models. Under strict 10/10 evaluation, *no* model exceeds 75%.
3. **Presence constraints are the hardest category.** 44% accuracy. Worse than
   any other implicit-constraint family they tested.

The framing they use — diagnose → measure → bridge → treat — is a useful
template for our own evals.

## Implications for Claudette Home

### 1. Treat presence as a first-class state, not an inference

The world model needs explicit slots:
- Where is the user? (room, posture if known)
- Where is each addressable device/object? (kettle, oven, front door, car)
- What is each in the middle of? (preheating, locked, charging, off)

The brain should query these slots *before* the LLM gets the prompt.
Don't ask the model to infer presence from language — feed presence as
context and let the model reason about action.

### 2. Build a presence-constraints eval before we ship anything

Variants of the car-wash class, scoped to a home:
- "Should I walk or drive to the supermarket to do the weekly shop?" (need the
  car for the bags, even if it's nearby)
- "Pop the kettle on" (kettle must have water; user must want tea/coffee soon)
- "Lock up before bed" (everyone who needs to be inside is inside)
- "Start the oven" (something to cook is going in; oven is empty/clean)

These should run against every candidate local model (Gemma 4, Phi family,
Voxtral pipeline, anything for the Karpathy Loop). The 44% HOB number is our
floor — anything below it is a no-ship.

### 3. Recording-engineer angle: presence is also acoustic

We already plan for barge-in, prosody, sub-300ms latency. Add to the list:
**room-presence detection** as an audio feature, not just a calendar/state
feature. Footsteps, breathing, the kettle's own boil signature, the front door.
A mic array that knows *who is in the room* sidesteps a whole class of
presence-constraint failures by giving the model true rather than inferred
context.

### 4. Don't trust the model to ask

Current LLMs almost never reply "wait — is the car with you?" They commit and
elaborate. Until that changes, the brain has to do the asking. Any action
whose presence constraints are unverified should route through a clarifying
turn before execution. Cheap, embarrassing-failure-preventing, and matches the
"local-first, fail-safe" ethos.

## Open questions

- Can we build a dedicated **presence verifier** as a small, fast model that
  sits in front of the main LLM? Single job: given (utterance, world-state),
  output the list of presence constraints the utterance assumes.
- Does the Karpathy Loop's self-improvement give us a path to *learn*
  household-specific presence constraints over time? (The kettle is always on
  the left hob. Fiona's keys live by the door. The car is parked on
  Archbishop Street except Sundays.)
- How do we test this without instrumenting the whole flat? Synthetic
  household traces, probably. Worth a separate note.

## Related

- \`docs/runtime-architecture.md\` — where the world-state slots will need to live
- Karpathy Loop notes (TBD) — self-improvement on household-specific constraints
- Voxtral / Kenneth — embedded TTS/STT, the small-model end of this stack
