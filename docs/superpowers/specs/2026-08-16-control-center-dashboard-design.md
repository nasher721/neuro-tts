# Neuro ICU TTS Control Center Dashboard Design

## Status

Approved during brainstorming on 2026-08-16. This document defines the first usability-focused slice of the broader Neuro ICU TTS overhaul.

## Goal

Make the add-on understandable and operable for both technical maintainers and nontechnical users through a dashboard-first Control Center. The first screen should answer three questions quickly:

1. Is the engine working?
2. What scope and queue state are active?
3. Is anything asking for attention?

The interface should expose advanced controls without making ordinary operation feel technical.

## Product direction

Use an operational dashboard with progressive disclosure. Returning users open directly to Overview. Users with incomplete setup see the same dashboard structure with a prominent setup banner and checklist. Detailed configuration, queue management, diagnostics, and maintenance remain separate views.

This direction was selected over a single long page because it preserves discoverability without allowing the Overview to become crowded. It was selected over explicit Simple/Advanced modes because progressive disclosure avoids a mode-switching concept while still serving both audiences.

## Information architecture

The Control Center has these navigation areas:

- **Overview** — health, scope, queue, attention, activity, and recommended next action.
- **Scope & automation** — pilot/full mode, deck or tag scope, scan behavior, and automation settings.
- **Voice & engine** — F5-TTS paths, model, reference voice, device, conversion settings, validation, and test generation.
- **Queue** — searchable and filterable note-level jobs with progress, retry, cancel, regenerate, and details.
- **Diagnostics** — structured errors, bounded subprocess output, dependency checks, and diagnostic export.
- **Maintenance** — read-only media audit, cleanup preview, migration status, and explicitly confirmed maintenance actions.

### Overview layout

The Overview contains five summary cards:

- **Engine:** ready, unavailable, or needs attention; includes the last successful test.
- **Scope:** pilot/full mode, selected deck or tag, and eligible-note count.
- **Queue:** queued, generating, completed, and paused counts.
- **Attention needed:** failed or skipped notes with the highest-priority issue.
- **Activity:** last scan, last generation, and recent system event.

Below the cards, show one contextual **Recommended next action**, such as running an engine test, selecting a pilot scope, reviewing queued notes, or inspecting failures.

The Overview never contains the complete note-level queue. It links to Queue for search, filters, and per-note actions.

## First-run and status behavior

The dashboard banner is state-aware:

- **Not configured:** prompt the user to set up the engine.
- **Configured but unvalidated:** prompt the user to run a test generation and identify dependency warnings.
- **Ready:** show the normal health, scope, queue, attention, and activity cards.
- **Degraded:** keep the dashboard usable while identifying the affected subsystem and linking to its fix.

“Ready” means the configured F5-TTS engine, reference voice, output conversion, and device have passed validation and produced one test clip. It does not require generating a real note.

Every status includes a plain-language label, a short explanation, a specific next action, and an optional Advanced details disclosure. Raw logs and paths are secondary details, not the primary error presentation.

Example:

> FFmpeg was not found. Install or configure FFmpeg, then run the engine test again.

The interface refreshes after scans, generations, validation, configuration changes, and sync completion, but never blocks while synthesis runs.

## Component and data flow

The Qt dashboard consumes a read-only status model and does not directly access Anki collections, SQLite, media, or subprocesses.

```text
Anki hooks / worker / queue
          ↓
     Status service
          ↓
   Overview view model
          ↓
      Qt dashboard
```

Responsibilities:

- **Status service:** aggregate engine health, scope counts, queue counts, failures, and recent activity.
- **Overview view model:** convert internal state into user-facing labels, descriptions, and recommended actions.
- **Qt dashboard:** render cards, banners, navigation, and refresh states only.
- **Detail views:** own commands and forms for their areas, such as retrying a queue item or validating the engine.
- **Event updates:** queue and engine events trigger lightweight dashboard refreshes on Anki’s main thread.

The dashboard loads a snapshot when opened and responds to status events. Dashboard actions dispatch application-layer commands; they never perform synthesis directly.

## Error handling

User-facing errors are grouped into:

- **Setup errors:** missing paths, dependencies, reference audio, or invalid configuration.
- **Operational errors:** synthesis failures, timeouts, cancellations, stale results, or unavailable devices.
- **Data/scope warnings:** skipped notes, unsupported note types, missing fields, or pilot-tag mismatches.

Each error offers an appropriate safe action: fix configuration, retry, view affected notes, open diagnostics, or dismiss until the next occurrence. The Overview shows counts and summaries. Diagnostics stores bounded technical details and subprocess output.

## Testing strategy

Test the dashboard without requiring a live F5-TTS process by using status fixtures and fake event sources. Coverage should include:

- Status aggregation from mixed engine and queue states.
- Recommended-action selection for each setup and operational state.
- Transitions from setup to validated, ready, and degraded states.
- Plain-language mapping for representative errors.
- Refresh behavior after queue and validation events.
- Non-blocking behavior while status-producing work runs.
- Command and scope boundaries for detail-view actions.

Manual acceptance should verify that a first-time user can identify the next action, a returning user can understand current health at a glance, and a technical maintainer can reach advanced details without editing source or raw JSON.

## Scope boundaries

This design covers the dashboard and its status presentation. It does not by itself define the persistent queue schema, F5-TTS worker implementation, reviewer toolbar, or migration protocol. Those subsystems remain governed by the existing overhaul task and should expose stable contracts to the dashboard.

The initial implementation slice should be a read-only dashboard backed by fixtures or existing state, followed by live queue and engine-validation integration once those contracts are stable.

