# Claudette Home Runtime Architecture

## Purpose

This document captures the intended runtime architecture for **Claudette Home** on dedicated, headless hardware.

Prototype target:
- **Hardware:** Raspberry Pi (16 GB RAM)
- **Audio:** attached audio HATs / microphone / speaker path
- **Mode:** headless Linux appliance
- **UI:** custom Claudette product UI
- **Automation backbone:** Home Assistant
- **AI strategy:** local-first for latency and resilience, cloud escalation when needed

---

## Core Architecture Principle

**Home Assistant does the automation heavy lifting. Claudette does the interaction.**

That means:
- **Home Assistant** owns devices, automations, scenes, schedules, state, and integrations
- **Claudette** owns wake word behavior, conversation style, low-latency interaction, ambiguity handling, and model-routing decisions
- **Local AI** handles fast household interactions and offline mode
- **Cloud AI** is used only when a request exceeds local capability or requires internet knowledge

This avoids trying to turn Home Assistant into a personality engine and avoids rebuilding a home automation platform from scratch.

---

## System Roles

### 1. Home Assistant
Home Assistant is the house operating system / automation engine.

Responsibilities:
- device registry
- entity state
- scenes
- automations
- schedules
- events and triggers
- Zigbee / Matter / integration ecosystem
- service-call execution

Home Assistant is expected to run **headless on our own hardware**.

### 2. Claudette Interaction Layer
This is the product layer we own.

Responsibilities:
- wake word handling
- conversational state
- natural-language interpretation
- response style / personality
- latency management
- deciding whether to stay local or escalate to cloud
- presenting status and control in the custom UI

### 3. Local AI Layer
This is the reflex system.

Candidate models:
- Gemma 4 or another compact local model suited for short command understanding and low-latency household interaction

Responsibilities:
- local command understanding
- short conversational turns
- clarification questions
- intent classification
- offline operation
- privacy-sensitive command handling

### 4. Cloud AI Layer
This is optional and only used when needed.

Responsibilities:
- web-backed knowledge
- long-form reasoning
- richer summarization
- complex drafting/generation
- any capability that depends on live internet access

---

## Design Principle: Local Reflexes, Cloud Cognition

The system should not depend on one model doing everything.

Instead:
- **Local reflexes** for fast, routine, private, household actions
- **Cloud cognition** for broad knowledge, research, and complex reasoning

Examples of **local-first** interactions:
- "Claudette, lights off"
- "Claudette, set dinner mode"
- "Claudette, what’s on downstairs?"
- "Claudette, did the front door open?"

Examples of **cloud-escalated** interactions:
- "Claudette, summarize today’s AI news"
- "Claudette, compare these options"
- "Claudette, draft a reply"
- "Claudette, check the ferry status"

---

## Offline Mode

Claudette Home must support a mode that can operate **without internet access**.

Offline mode should still support:
- wake word detection
- VAD / audio turn detection
- local STT where feasible
- local intent classification
- short local dialogue
- Home Assistant service calls
- basic state queries
- local UI interaction

Offline mode does **not** need to support:
- open web research
- cloud-only reasoning
- remote API dependencies
- internet-backed summarization

This gives the system:
- resilience
- privacy
- lower latency
- graceful degradation during network outages

---

## Proposed Runtime Flow

1. **Wake word detected**
   - always local
   - e.g. "Claudette"

2. **Audio capture begins**
   - local microphone pipeline
   - VAD / turn-end detection

3. **Speech-to-text**
   - local where possible
   - cloud optional if higher-quality mode is explicitly allowed

4. **Intent routing**
   - determine whether request is:
     - home-control local
     - local conversational
     - cloud-enhanced

5. **Execution path**
   - home control → Home Assistant service/event/state path
   - local conversational → local model
   - cloud-enhanced → remote model/search stack

6. **Response generation**
   - fast local response when possible
   - spoken reply via TTS
   - mirrored in custom UI if appropriate

---

## Why Home Assistant Fits This Stack

Home Assistant is suitable because it already provides:
- mature automation primitives
- broad hardware/device support
- strong event/state model
- local control possibilities
- headless deployment on Linux hardware
- a clean separation between automation engine and external control layers

For Claudette Home, HA should be treated as the **automation substrate**, not the full user experience.

---

## Prototype Deployment Notes

Prototype target:
- Raspberry Pi 16 GB
- audio HATs
- headless deployment
- dedicated device role

Preferred stack shape:
- Home Assistant on the prototype device or adjacent dedicated automation node
- Claudette voice + local AI pipeline on-device
- custom UI layered on top

Whether Home Assistant runs as HA OS, container, or another managed form should be decided separately, but the architecture assumes:
- **our own hardware**
- **headless operation**
- **custom UI, not stock HA as product face**

---

## Product Positioning Summary

Claudette Home is **not** just Home Assistant with a different skin.

It is:
- a custom conversational home product
- with its own UI and interaction design
- using Home Assistant as the automation/control engine
- and a local-first AI layer for speed, privacy, and resilience

Short version:

**Home Assistant runs the house. Claudette runs the conversation.**

---

## Open Questions

- exact local model choice for on-device interaction (Gemma 4 vs alternatives)
- local STT/TTS stack selection for offline mode
- process boundary between Home Assistant and Claudette services
- whether HA should live on the same Pi or a separate dedicated node in later phases
- how much of the state model is mirrored into the custom UI versus queried live from HA

---

## Current Decision Snapshot

Confirmed so far:
- own hardware
- headless Linux deployment
- Raspberry Pi prototype
- audio HATs
- custom Claudette UI
- Home Assistant for automation heavy lifting
- local-first interaction strategy
- optional cloud escalation
- explicit offline-capable mode
