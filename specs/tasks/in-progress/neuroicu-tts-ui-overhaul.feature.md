# Neuro ICU TTS Anki UI and Application Overhaul

## Type

Feature / architecture refactor

## Summary

Transform the current Neuro ICU TTS Anki add-on from a synchronous background utility into a reliable, discoverable desktop product with two coordinated surfaces:

1. A lightweight reviewer toolbar/status experience for playback and current-card actions.
2. A full Neuro ICU TTS Control Center for setup, scope, queue management, diagnostics, and maintenance.

The upgrade must preserve note content, review scheduling, mobile sync behavior, and existing F5-TTS generation while moving long-running work off Anki’s UI thread.

## Current state

The installed add-on is concentrated in `main.py`, `tts_core.py`, `config.json`, and three pure-function tests. It currently:

- polls the collection every five seconds;
- runs F5-TTS synchronously from the timer/menu path;
- stores settings as machine-specific JSON paths;
- exposes only `Tools → Sync Neuro ICU TTS Now` and a fixed `Ctrl+Alt+V` shortcut;
- has no durable queue, progress model, retry/cancel controls, setup validation, or reviewer status UI;
- removes all `[sound:...]` tags during managed-field replacement, including user-owned audio.

## Goals

- Keep Anki responsive during synthesis and reconciliation.
- Make setup health, scope, queue status, and failures understandable without reading logs or editing source.
- Provide discoverable reviewer controls for replay, generate, regenerate, pause/stop, and current-card prioritization.
- Support configurable voice/model/device/reference audio, pilot/full-deck scope, automation, retries, cancellation, and safe cleanup.
- Detect desktop and mobile-originated edits, deduplicate jobs, and discard stale synthesis results.
- Preserve user-authored audio and all unrelated note HTML/content.
- Make behavior recoverable across Anki restarts and safe across profile/sync lifecycle events.

## Non-goals

- Building a separate cloud service or multi-user backend.
- Running TTS on AnkiMobile/AnkiDroid; mobile remains a playback/sync client.
- Changing Anki scheduling algorithms, card content semantics, or the Enhanced Cloze model structure.
- Enabling concurrent F5-TTS jobs by default.

## User experience requirements

### Reviewer surface

Add a compact, non-blocking reviewer toolbar or status widget with:

- Replay current audio.
- Pause/stop playback.
- Generate current card.
- Regenerate current card.
- Cancel current-card generation.
- Current status: ready, queued, generating, stale, failed, or unavailable.
- Open Control Center.
- Configurable keyboard shortcuts with visible discoverability/help.

Reviewer controls must target the active card explicitly and fall back to menu/shortcut behavior if a particular Anki webview hook is unavailable. They must never block card display waiting for TTS.

### Control Center

Add `Tools → Neuro ICU TTS Control Center` with these sections:

- **Overview:** setup health, configured scope, status counts, last scan, last successful generation.
- **Scope & automation:** model, decks, field, pilot tag/mode, suspended-note policy, sync scan, edit debounce, reviewer auto-generation.
- **Voice & engine:** F5-TTS repo/Python/model/reference audio/transcript/device/NFE steps/ffmpeg, validation and test-generation actions.
- **Queue:** searchable/filterable note list, statuses, progress, current job, pause/resume, cancel, retry, regenerate, and final summaries.
- **Diagnostics:** structured errors, bounded subprocess output, logs, exported diagnostic report, dependency checks.
- **Maintenance:** read-only media audit, cleanup preview, grace-period cleanup, state reset, and migration status.

The UI must show actionable reasons for skipped notes and require explicit confirmation before expanding from pilot to full deck.

## Architecture

Use a modular monolith with ports/adapters:

```text
Anki/Qt hooks and UI
        ↓
Application services and command/event bus
        ↓
Eligibility, planning, persistent queue, settings, state
        ↓
Single background TTS worker → F5-TTS adapter → staged artifact
        ↓
Main-thread digest revalidation → atomic media/note commit
```

Suggested package boundaries:

```text
domain/          models, eligibility, text, naming, state machine
application/     commands, planner, queue service, settings, events
adapters/        Anki hooks/repository/reviewer/media, F5-TTS, process runner
infrastructure/  SQLite store, config store, migrations, worker, logging
ui/              control center, settings dialogs, reviewer controls
```

The domain/application layers must not import Qt, Anki collection objects, subprocess details, or F5-TTS internals.

### Queue and state

Use profile-scoped SQLite user data, not the Anki collection database or add-on installation directory. Jobs must include note ID, source digest, configuration/profile digest, priority, attempts, state, timestamps, lease, staged artifact, and bounded error output.

States: `queued`, `running`, `succeeded`, `failed_retryable`, `failed_terminal`, `cancel_requested`, `cancelled`, `stale`.

Use a single worker by default. Reviewer requests get higher priority than background scans. Coalesce duplicate requests and mark older jobs stale after repeated edits. Recover expired running jobs after restart.

### F5-TTS adapter

Define a stable engine port for validation, synthesis, cancellation, and shutdown. Keep the current CLI invocation behind the adapter. The worker must run the subprocess off the UI thread, enforce timeouts, capture bounded output, convert WAV to MP3, validate the artifact, and return a staged file.

All collection access and note/media mutation must remain on Anki’s main thread.

### Commit protocol

When synthesis completes:

1. Re-read the note on the main thread.
2. Recompute normalized source and configuration digest.
3. If changed, mark the job stale and discard/quarantine the artifact.
4. If unchanged, atomically install media and update only the add-on-owned marker block.
5. Update the note, media index, and job state.

Upgrade marker handling to preserve user-owned `[sound:...]` tags. Only remove/replace the versioned managed block. Include source and synthesis-profile identity in deterministic filenames or the digest.

### Configuration

Move hard-coded module constants into a schema-versioned Anki add-on configuration repository. Support path validation, environment overrides, relative-path resolution, live reload, and human-readable validation errors. Keep mutable queue state separate from settings.

## Stable contracts and lifecycle rules

Define these versioned Python contracts before parallel UI/queue work begins:

```python
@dataclass(frozen=True)
class GenerateNote:
    note_id: int
    priority: int
    force: bool = False

@dataclass(frozen=True)
class TtsStatusChanged:
    job_id: str
    note_id: int
    state: str
    message: str | None
    progress: float | None

class TtsEngine(Protocol):
    def validate(self, config: EngineConfig) -> ValidationResult: ...
    def synthesize(self, request: SynthesisRequest, output_dir: Path) -> SynthesisArtifact: ...
```

Commands flow UI → application services; status events flow services → UI. UI code may not access notes, media, SQLite, or subprocesses directly. Contract changes require a schema/version constant and compatibility handling within the same release.

Lifecycle rules:

- Only the Anki main thread may access `mw.col`, note objects, `mw.reset()`, Qt widgets, or Anki media APIs.
- The worker owns one SQLite connection and one active F5-TTS subprocess; the main thread never shares that connection.
- A profile session ID is attached to jobs/events; profile switches stop the old worker before opening new profile state.
- Sync callbacks only enqueue reconciliation through `QTimer.singleShot`; they never synthesize or commit directly.
- Shutdown stops new enqueueing, requests subprocess cancellation, waits up to 5 seconds, persists leases, and closes the worker/database.
- Expired running/committing jobs become retryable on next startup.
- UI callbacks ignore events from an old profile session or a no-longer-active reviewer card.

## Migration and commit safety

Before mutating notes, create a collection backup/export and a read-only inventory of note IDs, `Extra`, tags, decks, managed filenames, and media hashes. Recognize only the exact existing v1 managed marker plus its deterministic filename as add-on-owned. Preserve every other `[sound:...]` tag; ambiguous legacy audio remains untouched and is reported for review.

Use a recoverable two-phase protocol:

- **Prepare:** stage and validate the artifact, record a pending artifact row, and verify the current note digest.
- **Commit:** atomically move media into Anki media, update only the managed block, update the note on the main thread, then mark the job succeeded.
- **Recovery:** reconcile pending artifacts with note markers and media on startup; quarantine ambiguous files instead of deleting them.

Never delete media during ordinary generation. Cleanup is a separately confirmed, previewable action with a configurable grace period. A failed generation must not delete a valid existing managed file.

## Implementation process

### Phase 1 — Boundary extraction and safety baseline

- Add regression tests for normalization, managed markers, user-owned sound preservation, eligibility, and idempotency.
- Extract configuration, eligibility, F5-TTS execution, Anki repository, media installer, and logging boundaries.
- Add config schema/migration support and remove machine-specific constants.
- Fix duplicate hook/log-handler registration.
- Freeze the command/event/engine contracts and lifecycle ownership rules.
- Implement v1 marker inventory and v2 managed-block migration in dry-run mode.

**Exit criteria:** contracts are versioned; dry-run migration produces an inventory without collection mutation; tests prove unrelated audio and note content are preserved.

### Phase 2 — Persistent asynchronous queue

- Add SQLite schema, migrations, job state machine, leases, deduplication, priorities, retry/backoff, and restart recovery.
- Add single worker thread/process isolation and cancellation.
- Replace timer/menu synthesis with enqueue-only behavior.
- Add main-thread completion dispatcher and digest revalidation.
- Add two-phase artifact/commit recovery and startup reconciliation.

**Exit criteria:** enqueue/hook callbacks return within 100 ms in fixtures; synthesis does not block the reviewer; restart, cancel, retry, stale-edit, crash-recovery, and failure paths are tested.

### Phase 3 — Control Center

- Add Overview, Scope, Engine, Queue, Diagnostics, and Maintenance views.
- Add setup validation and test-generation action.
- Add searchable queue/status table with filters and actionable errors.
- Add pilot/full confirmation and read-only cleanup preview.

**Exit criteria:** a user can configure and operate the add-on without editing source or raw JSON.

### Phase 4 — Reviewer integration

- Add current-card toolbar/status widget.
- Add explicit Python/JS command bridge with active-card validation.
- Add current-card priority, generate/regenerate/cancel, playback controls, and configurable shortcuts.
- Add graceful menu/shortcut fallback.

**Exit criteria:** reviewer actions are discoverable, keyboard accessible, non-blocking, and never affect the wrong card.

### Phase 5 — Reconciliation, migration, and cleanup

- Add targeted editor/sync reconciliation with low-frequency safety scan.
- Migrate v1 managed markers/files to the safer ownership format.
- Add conservative grace-period media cleanup.
- Add startup/shutdown/profile-switch lifecycle handling.

**Exit criteria:** desktop edits, mobile-originated edits, sync interruptions, restarts, and cleanup are recoverable and safe.

### Phase 6 — Optional persistent F5 worker optimization

- Measure process-per-job performance.
- If needed, add a long-lived F5-TTS worker process with a versioned JSON protocol and CLI fallback.

**Exit criteria:** optimization is justified by measured throughput/latency and does not reduce reliability.

## Parallelization

After Phase 1 contracts are approved, these workstreams can proceed in parallel:

| Workstream | Owns | Depends on |
|---|---|---|
| Queue/infrastructure | SQLite, worker, state machine, retries | Phase 1 boundaries |
| F5-TTS adapter | process runner, timeout/cancel, artifact staging | Phase 1 engine port |
| Control Center UI | settings, overview, queue, diagnostics views | command/event contracts |
| Reviewer UI | toolbar, JS bridge, shortcuts, status | command/event contracts |
| Test harness | fake engine, Anki mocks, Qt/integration fixtures | domain contracts |

Integration order: queue + engine + committer, then Control Center and reviewer UI, then migration/cleanup and end-to-end validation.

## Verification and release gates

### Automated tests

- Domain normalization, digest, naming, eligibility reason codes, managed/unmanaged audio, idempotency.
- Queue deduplication, priority, retry/backoff, cancellation, leases, restart recovery, stale transitions.
- Fake F5-TTS success, nonzero exit, timeout, malformed/missing output, ffmpeg failure, cancellation.
- Main-thread-only Anki writes and changed-during-generation behavior.
- Config schema/migrations/path validation/live reload.
- Media ownership, cleanup grace period, and unrelated-media protection.
- Qt/menu/shortcut/reviewer command tests.

### Manual acceptance

Use a disposable Anki profile with exactly 20 fixed pilot notes covering HTML, images, Unicode, medical abbreviations, long text, existing user audio, nested decks, wrong models, missing fields, and ambiguous legacy audio.

Verify:

- Setup health is understandable from Overview.
- Reviewer controls replay, pause/stop, generate, regenerate, cancel, and status correctly.
- Anki remains responsive during generation.
- Hook callbacks and reviewer commands return in under 100 ms; no main-thread work blocks for more than 250 ms outside normal Anki rendering.
- Reviewer card changes during generation never display or commit the prior card’s job status.
- Restarting during prepare/commit boundaries recovers without duplicate media or note corruption.
- Mobile edit → sync → desktop regeneration works.
- Repeated scans are idempotent.
- Failed generation leaves notes and valid existing audio unchanged.
- Full-deck mode requires confirmation and displays impact estimates.
- Cleanup shows a preview and never deletes unrelated audio.
- Keyboard-only navigation, visible focus, accessible labels, dark mode, high contrast, larger text, and reduced motion work.

Run the UI acceptance suite against the current Anki Desktop release and the immediately previous release, including fallback behavior when reviewer webview hooks are unavailable.

### Rollout gates

Pilot passes only when exactly 20/20 intended notes succeed, zero unintended notes change, zero user-owned audio tags are lost, mobile round-trip passes, failures are retryable/visible, crash-injection boundaries recover, and no collection/reviewer regressions remain.

Full-deck backfill requires a collection backup, read-only inventory, cloned-profile rehearsal, media/note invariants, and verified rollback.

## Main risks and decisions

- Keep a modular monolith; a separate service is unnecessary for a single-user desktop add-on.
- Serialize F5-TTS by default because MPS memory and model startup make parallelism risky.
- Keep all Anki writes on the main thread; workers return staged artifacts only.
- Use source plus synthesis-profile digests for job identity and stale detection.
- Preserve user audio by managing only a versioned add-on-owned marker block.
- Store operational state in profile-scoped SQLite and settings in Anki’s add-on configuration.
- Do not log full medical note text by default.

## Definition of done

- Reviewer UI and Control Center are implemented and integrated.
- Queue survives restart and never blocks Anki’s UI.
- F5-TTS failures, cancellation, retry, and stale edits are safe.
- User-authored audio and note content are preserved.
- Desktop/mobile sync behavior is validated.
- Accessibility and migration tests pass.
- Documentation explains setup, scope, recovery, and rollback.
- Pilot release is approved before full-deck mode is enabled.
