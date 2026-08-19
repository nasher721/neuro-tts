---
title: Implement Config Center
depends_on: neuroicu-tts-ui-overhaul.feature.md
skill: .claude/skills/anki-addon-config/SKILL.md
analysis: .specs/analysis/analysis-config-center.md
design: .specs/plans/neuroicu-tts-config-center.design.md
---

## Initial User Prompt

Implement the Config Center per the approved design at `.specs/plans/neuroicu-tts-config-center.design.md` (locked decisions Q1/Q2/Q3, Approach #1: new pure `addon/config.py` + ControlCenter gains QTabWidget; Core MVP scope — G1/G8/G11 full, G2/G3/G4/G5 lighter read-only views; locked engine, safe knobs only).

## Description

Add a Config Center inside the Control Center so ICU clinicians can configure TTS speed, audio device, and ffmpeg path safely — without editing `config.json` by hand or touching engine internals.

**Current-state reality (from revised analysis):** a partial `addon/config.py` already exists (179 lines) but is **unused dead code** — `main.py` still runs on its own legacy `_config()`/`CFG`/module-level constants. The existing module also diverges from the design: `SCHEMA_VERSION = 1` (design: 2), no `validate()`, unknown keys dropped on load (design: preserve), and an unenforced `EDITABLE_KEYS` that wrongly includes locked engine keys. This task therefore **upgrades and adopts** the existing module rather than creating one from scratch, and migrates `main.py` onto it.

The versioned `config.py` owns schema version, defaults, validation, migration, atomic writes, and getters. The Control Center gains a `QTabWidget` with six tabs: Overview (existing dashboard), Settings (full form), Queue (read-only), Diagnostics (engine test + log tail), Maintenance (clear finished jobs + storage size), and Scope (pilot toggle + full-deck action).

**Config-key mapping (normative):** UI field "Speed" ↔ key `f5_speed` (float 0.5–2.0); "Device" ↔ `f5_device` (`cpu|mps|cuda`); "ffmpeg path" ↔ `ffmpeg_path`; "Pilot only" ↔ `pilot_only`; "Pilot tag" ↔ `pilot_tag`. Locked read-only: `f5_tts_repo`, `f5_tts_python`, `f5_ref_audio`, `f5_ref_text`, `f5_model`, `f5_nfe_step`. Existing key names are kept so live user configs migrate without renaming.

**Scope boundaries (Core MVP):**

| User story | Scope |
|---|---|
| G1 — Settings form | Full: load, save, revert, reload, inline validation |
| G8 — Config validation/migration | Full: v1→v2 migration, schema validation |
| G11 — Full-deck conversion | Full: impact dialog + confirmation + job enqueue |
| G2 — Queue view | Read-only: note counts, status rows, no mutation controls |
| G3 — Diagnostics | Read-only: engine test callback + log tail |
| G4 — Maintenance | Light: clear finished jobs + display storage size |
| G5 — Scope toggle | Light: pilot-only toggle, no full-deck controls beyond G11 |

Engine fields are locked read-only in the UI and rejected by `config.save()`. Safe fields (`f5_speed`, `f5_device`, `ffmpeg_path`) are re-read by the worker at next job start; running jobs are never interrupted.

**Out of scope:** `vendor/F5-TTS/` internals; the stale duplicate `neuroicu_tts_addon/` tree; new queue machinery; `addon/README.md` rewrite beyond a short Config Center note.

## Acceptance Criteria

### G1 — Settings form (full)

**G1.1** Given the Settings tab is open, When the add-on loads, Then all fields display current values from `config.json` (editable fields for `f5_speed`, `f5_device`, `ffmpeg_path`; read-only fields for engine keys).

**G1.2** Given the Settings tab is open, When the user changes `f5_speed` to `1.5` and clicks Save, Then `config.json` is atomically updated and `config.get_speed()` returns `"1.5"` on next read.

**G1.3** Given the user made unsaved changes, When the user clicks Revert, Then all fields revert to the last saved values and no file write occurs.

**G1.4** Given an external edit to `config.json`, When the user clicks Reload, Then the Settings form refreshes to reflect the external changes and a success message is shown.

**G1.5** Given the user enters `f5_speed` = `5.0`, When they click Save, Then an inline validation error appears ("speed must be between 0.5 and 2.0"), the file is not written, and the field retains the invalid value until corrected.

**G1.6** Given the user enters `f5_device` = `"tpu"`, When they click Save, Then an inline validation error appears ("device must be cpu, mps, or cuda") and the file is not written.

**G1.7** Given the user enters a non-existent `ffmpeg_path`, When they click Save, Then an inline validation error appears and the file is not written.

**G1.8** Given the user enters a `pilot_tag` with whitespace, When they click Save, Then an inline validation error appears ("pilot_tag must be non-empty with no whitespace") and the file is not written.

### G2 — Queue view (read-only)

**G2.1** Given the Queue tab is open, When the user views the tab, Then it displays note counts by status (queued, running, succeeded, failed) as a read-only list without mutation controls.

### G3 — Diagnostics (read-only)

**G3.1** Given the Diagnostics tab is open, When the user clicks "Engine Test", Then the add-on runs the existing engine test callback (`_start_engine_test`) and displays pass/fail with an error banner on failure.

**G3.2** Given the Diagnostics tab is open, When the user views the log tail, Then the last N lines of `neuroicu_tts.log` are displayed.

### G4 — Maintenance (light)

**G4.1** Given the Maintenance tab is open, When the user views the tab, Then it displays total storage size used by generated audio files (media dir files matching the managed prefix).

**G4.2** Given the Maintenance tab is open, When the user clicks "Clear Finished", Then all jobs in `succeeded` or `failed_terminal` state are removed from the JobStore and the count updates.

### G5 — Scope toggle (light)

**G5.1** Given the Scope tab is open, When the user toggles pilot-only on/off, Then the `pilot_only` value in `config.json` updates atomically and the Overview reflects the new scope.

### G8 — Config validation and migration (full)

**G8.1** Given a v1 `config.json` (flat, no `schema_version`), When the add-on loads, Then `migrate()` fills missing keys from `DEFAULTS`, sets `schema_version` to 2, and preserves any unknown keys.

**G8.2** Given a corrupt or unreadable `config.json`, When the add-on loads, Then `load()` falls back to `DEFAULTS` without raising and logs a warning.

**G8.3** Given `config.json` with valid data, When `validate()` runs, Then it returns an empty error list.

**G8.4** Given `config.json` with `f5_speed` out of range and a `pilot_tag` containing spaces, When `validate()` runs, Then it returns two distinct error strings, one per violation.

**G8.5** Given `config.save()` is called with a locked engine key (e.g. `f5_model`) or an unknown key, When save runs, Then the key is rejected/ignored and the persisted file is unchanged for that key.

### G11 — Full-deck conversion with impact confirmation (full)

**G11.1** Given the Scope tab is open and deck notes exist, When the user clicks "Full-Deck Convert", Then an ImpactDialog appears showing note count and estimated runtime.

**G11.2** Given the ImpactDialog is displayed, When the user clicks Confirm, Then one job per note in the deck is enqueued via the existing JobStore and a success banner shows "N notes queued".

**G11.3** Given the ImpactDialog is displayed, When the user clicks Cancel, Then no jobs are enqueued and the dialog closes.

**G11.4** Given a mid-loop failure during enqueue, When fewer than all notes are enqueued, Then a banner shows "N of M notes queued" and no partial state is left inconsistent.

**G11.5** Given duplicate digests already in the JobStore, When full-deck enqueue runs, Then duplicates are skipped (deduped by digest via `JobStore.existing_digests()`) and no duplicate jobs appear.

## Architecture Overview

### Solution strategy

Put the seam at config, not UI. `addon/config.py` (existing, upgraded) is the single module that reads and writes `config.json`; `main.py` abandons its legacy `_config()`/`CFG` block (L24–37) and consumes typed getters. The Control Center stays a pure renderer — every side effect is an injected callback, and every callback runs through the existing `_run_callback` containment wrapper (disable → run → banner on failure → re-enable → refresh).

### Key decisions

1. **Adopt-and-upgrade over rewrite** — the existing `addon/config.py` already has correct atomic save, mtime reload, and ffmpeg resolution. Upgrade it to the design spec (schema v2, `validate()`, unknown-key-preserving `migrate()`, enforced `EDITABLE_KEYS`) instead of replacing it. *Trade-off:* careful diffing against the partial implementation, but no duplicated logic survives.
2. **Constants → getters, at point of use** — `F5_SPEED`/`F5_DEVICE` module constants in `main.py` become `config.get_speed()`/`config.get_device()` calls inside `_run_f5_inference`, so saved changes reach the worker at next job start without restarting Anki. Locked engine constants (`F5_TTS_REPO`, `F5_TTS_PYTHON`, `F5_MODEL`, `F5_REF_AUDIO`, `F5_REF_TEXT`, `F5_NFE_STEP`) stay import-time constants — they are engine-locked by design.
3. **Single write path** — `_update_pilot_tag`'s ad-hoc atomic write (L624–646) is rerouted through `config.save()`; no other code writes `config.json`.
4. **Tab renderer pattern** — each tab is a small Qt widget built by the dialog; logic callbacks (`on_save_settings(dict) -> list[str]`, `on_clear_finished() -> int`, `on_full_deck_convert() …`) are injected from `main.py`, keeping Qt code thin and logic unit-testable.
5. **G11 reuses JobStore** — no new queue machinery; dedupe by digest via a new `JobStore.existing_digests()`.

### Expected changes

| File | Change |
|---|---|
| `addon/config.py` | Upgrade: `SCHEMA_VERSION = 2`; add `validate()`; rewrite `_load`/migrate to preserve unknown keys; enforce `EDITABLE_KEYS = {f5_speed, f5_device, ffmpeg_path, pilot_only, pilot_tag}` in `save()`; add validation errors return path |
| `addon/test_config.py` | **New**: validation, migration, fallback, atomic save, reload, editable-keys guard |
| `addon/main.py` | Remove `_config()`/`CFG` legacy block; delegate `_ffmpeg_path()`; getters at point of use in `_run_f5_inference`; reroute `_update_pilot_tag`; extend `JobStore` (`counts_by_status`, `clear_finished`, `existing_digests`); add tab callbacks; replace `_dashboard_unavailable` stubs |
| `addon/control_center.py` | Restructure into `QTabWidget` (6 tabs); move Overview verbatim into tab 1; build Settings/Queue/Diagnostics/Maintenance/Scope tabs; `ImpactDialog` |
| `addon/test_main.py` | Migrate `addon.main.CFG`/`F5_*` patch sites (~33) to `addon.config` getters; add JobStore extension tests; G11 callback tests with fake `col.find_notes` |
| `addon/README.md` | Short note: configure via Tools → Neuro ICU TTS Control Center (replaces "edit config.json") |

### References

- Design: `.specs/plans/neuroicu-tts-config-center.design.md` (locked decisions Q1/Q2/Q3)
- Analysis: `.specs/analysis/analysis-config-center.md` (verified line numbers, risk table)
- Skill: `.claude/skills/anki-addon-config/SKILL.md`

## Implementation Process

### Step 1 — Upgrade `addon/config.py` to design spec (Foundation)

- **Agent**: opus | **Estimate**: Medium | **Parallel with**: Step 2
- **Goal**: single correct config module implementing G8.
- **Subtasks**:
  1. Bump `SCHEMA_VERSION` to 2; split `_ensure_versioned` into `migrate(raw)` that fills defaults **and preserves unknown keys**.
  2. Add `validate(raw) -> list[str]`: `f5_speed` in [0.5, 2.0]; `f5_device` in {cpu, mps, cuda}; `ffmpeg_path` empty-or-existing file; `pilot_tag` non-empty, no whitespace.
  3. Redefine `EDITABLE_KEYS = frozenset({"f5_speed", "f5_device", "ffmpeg_path", "pilot_only", "pilot_tag"})`; `save(updates)` validates the merged dict, rejects non-editable keys, and returns/raises validation errors without writing.
  4. Keep `reload_config_if_changed`, `get_speed` (str), `get_device`, `get_ffmpeg_path` as-is.
- **Success criteria**: `test_config.py` passes covering G8.1–G8.5; corrupt-JSON fallback logs warning and never raises; existing 49 tests still pass.
- **Output**: `addon/config.py`, `addon/test_config.py`.

### Step 2 — Extend `JobStore` (Foundation)

- **Agent**: sonnet | **Estimate**: Small | **Parallel with**: Step 1
- **Goal**: queue primitives needed by G2/G4/G11, in `main.py` L112–130.
- **Subtasks**:
  1. `counts_by_status() -> dict[str, int]` — GROUP BY state.
  2. `clear_finished() -> int` — delete `succeeded`/`failed_terminal` rows, return count.
  3. `existing_digests() -> set[str]` — all digests currently stored.
- **Success criteria**: unit tests for all three methods pass against a temp sqlite DB; no change to existing JobStore behavior.
- **Output**: `addon/main.py` (JobStore methods), tests in `test_main.py`.

### Step 3 — Migrate `main.py` onto `config.py` (Core)

- **Agent**: opus | **Estimate**: Large | **Depends on**: Step 1
- **Goal**: one config source; live-reload of safe fields reaches the worker.
- **Subtasks**:
  1. Delete `_config()` and `CFG`; locked engine constants read once from `config.get(...)` at import.
  2. `_run_f5_inference` uses `config.get_device()` / `config.get_speed()` per invocation.
  3. `_ffmpeg_path()` delegates to `config.get_ffmpeg_path()`.
  4. `_update_pilot_tag` rerouted through `config.save({"pilot_tag": value})`; keep whitespace validation and banner semantics.
  5. Migrate all `test_main.py` patch sites from `addon.main.CFG`/`F5_*` to `addon.config` getters (grep for every site; batch rename; re-run suite).
- **Success criteria**: full suite green; `grep -n "CFG" addon/main.py` returns no legacy config reads; worker test proves changed speed is picked up at next job start.
- **Risk**: missing a mock site → silent real-config reads. *Mitigation*: grep-driven rename, suite run after each batch.

### Step 4 — ControlCenter `QTabWidget` restructure (Core)

- **Agent**: opus | **Estimate**: Medium | **Depends on**: Step 1
- **Goal**: six-tab shell; Overview behavior unchanged.
- **Subtasks**:
  1. Move existing Overview content verbatim into Tab 1.
  2. Add empty tab shells: Settings, Queue, Diagnostics, Maintenance, Scope — each wired to injected callbacks, each callback through `_run_callback`.
  3. Preserve the Qt-import guard, palette-only styling, and `setAccessibleName` coverage.
- **Success criteria**: smoke checklist passes (open → 6 tabs render → Overview refresh/recommended action/scan/current still work); pure tests still import the module without Anki.

### Step 5 — Settings tab (G1, full)

- **Agent**: opus | **Estimate**: Medium | **Depends on**: Steps 3, 4
- **Subtasks**: `QFormLayout` with editable `f5_speed`, `f5_device` (dropdown), `ffmpeg_path`; locked engine fields read-only; Save/Revert/Reload buttons; inline validation errors from `config.save()`; reload calls `reload_config_if_changed()`.
- **Success criteria**: G1.1–G1.8 verified in smoke test; invalid saves write nothing.

### Step 6 — Queue tab (G2, read-only)

- **Agent**: sonnet | **Estimate**: Small | **Depends on**: Steps 2, 4
- **Subtasks**: counts by status from `JobStore.counts_by_status()` + read-only recent-jobs rows; refresh on tab switch.
- **Success criteria**: G2.1; no mutation controls present.

### Step 7 — Diagnostics tab (G3, read-only)

- **Agent**: sonnet | **Estimate**: Small | **Depends on**: Step 4
- **Subtasks**: "Engine Test" button wired to existing `_start_engine_test`; log tail reads last N lines of `neuroicu_tts.log`; failure → banner.
- **Success criteria**: G3.1, G3.2.

### Step 8 — Maintenance tab (G4, light)

- **Agent**: sonnet | **Estimate**: Small | **Depends on**: Steps 2, 4
- **Subtasks**: storage size of managed media files; "Clear Finished" → `JobStore.clear_finished()` → updated count.
- **Success criteria**: G4.1, G4.2.

### Step 9 — Scope tab + G11 full-deck conversion

- **Agent**: opus | **Estimate**: Medium | **Depends on**: Steps 2, 3, 4
- **Subtasks**:
  1. Pilot-only toggle → `config.save({"pilot_only": …})`; Overview reflects new scope.
  2. `ImpactDialog`: note count via `col.find_notes("deck:…")`; pure `estimate_runtime(n, per_note_avg)`; Confirm/Cancel.
  3. Confirm → per-note enqueue with digest dedupe via `existing_digests()`; per-note try/except → "N of M notes queued" banner.
  4. Replace `_dashboard_unavailable` stubs with real tab-switch actions.
- **Success criteria**: G5.1, G11.1–G11.5; callback-level test with fake `col.find_notes` enqueues exactly N deduped jobs.

### Step 10 — Integration, smoke test, docs (Polish)

- **Agent**: sonnet | **Estimate**: Small | **Depends on**: Steps 5–9
- **Subtasks**: full suite + manual smoke checklist; `addon/README.md` Config Center note; document same-second-mtime edge case.
- **Success criteria**: Definition of Done met.

### Implementation summary

| Step | Focus | Agent | Estimate | Depends on |
|---|---|---|---|---|
| 1 | config.py upgrade + tests | opus | Medium | — |
| 2 | JobStore extensions | sonnet | Small | — |
| 3 | main.py migration | opus | Large | 1 |
| 4 | QTabWidget restructure | opus | Medium | 1 |
| 5 | Settings tab (G1) | opus | Medium | 3, 4 |
| 6 | Queue tab (G2) | sonnet | Small | 2, 4 |
| 7 | Diagnostics tab (G3) | sonnet | Small | 4 |
| 8 | Maintenance tab (G4) | sonnet | Small | 2, 4 |
| 9 | Scope tab + G11 | opus | Medium | 2, 3, 4 |
| 10 | Integration + docs | sonnet | Small | 5–9 |

### Risks

| Risk | Priority | Mitigation |
|---|---|---|
| Worker breaks when constants become getters mid-run | High | Safe fields re-read at job start only; locked constants captured at import |
| Missed test-mock site reads real config | High | Grep-driven rename; suite after each batch (Step 3) |
| `save()` key rejection breaks pilot-tag flow | Medium | `pilot_tag` in `EDITABLE_KEYS`; single write path |
| QTabWidget restructure regresses Overview | Medium | Verbatim move; smoke checklist in Step 4 |
| G11 partial enqueue leaves inconsistency | Medium | Per-note try/except; "N of M" banner; digest dedupe |
| Same-second mtime miss on reload | Low | Documented edge case |

### Definition of Done

- All acceptance criteria G1–G5, G8, G11 verified (unit tests for pure logic, smoke checklist for Qt).
- Full test suite green (49 existing + new config/JobStore/G11 tests).
- No legacy `CFG` reads remain in `main.py`; `config.py` is the only writer of `config.json`.
- Smoke test: open Control Center → 6 tabs render → save persists → reload picks up external edit → full-deck convert enqueues deduped jobs.

## Parallelization

### Execution waves

```
Wave 1 (parallel):        Step 1  ‖  Step 2
Wave 2 (parallel):        Step 3  ‖  Step 4
Wave 3 (parallel):        Step 5  ‖  Step 6  ‖  Step 7  ‖  Step 8  ‖  Step 9
Wave 4 (sequential):      Step 10
```

- **Max parallelization depth**: 5 (Wave 3).
- **Agent tiers**: opus = architecture-sensitive (config core, main.py migration, tab shell, settings, G11); sonnet = mechanical/high-volume (JobStore extensions, read-only tabs, mock renames, docs).

### Sub-agent execution directive

- Steps in the same wave **MUST** be executed in parallel by separate agents.
- Each agent **MUST** read this task file plus `.specs/analysis/analysis-config-center.md` and `.claude/skills/anki-addon-config/SKILL.md` before editing.
- Wave N+1 **MUST NOT** start until every step in wave N has passed its verification.
- Steps 1 and 4 both touch `addon/control_center.py`/`addon/config.py` boundaries — Step 4 **MUST NOT** modify `config.py`; Step 1 **MUST NOT** modify `control_center.py`.
- Step 3 owns `addon/main.py` in Wave 2; Step 4 owns `addon/control_center.py` — no shared-file conflicts within a wave.

## Verifications

**Default threshold: 4.0/5.0** per evaluation. Levels: Panel = multi-judge; Single = one judge; Per-Item = one evaluation per subtask; None = manual smoke only.

### Step 1 — config.py upgrade — **Panel** (HIGH criticality: single write path for user config)

Rubric (weights sum 1.0):
1. Validation correctness (0.30) — all four rules implemented; boundary values 0.5/2.0 accepted, 0.49/2.01 rejected.
2. Migration fidelity (0.25) — v1→v2 fills defaults, stamps version, preserves unknown keys (G8.1).
3. Failure safety (0.25) — corrupt JSON → defaults + warning, never raises (G8.2); invalid save writes nothing (G8.5).
4. Test coverage (0.20) — G8.1–G8.5 each have a dedicated passing test.

### Step 2 — JobStore extensions — **Single** (MEDIUM)

Rubric:
1. SQL correctness (0.40) — counts match GROUP BY semantics; clear targets exactly `succeeded`/`failed_terminal`.
2. Return contracts (0.30) — `clear_finished` returns deleted count; `existing_digests` returns a set.
3. Non-regression (0.30) — existing enqueue/recoverable behavior untouched, suite green.

### Step 3 — main.py migration — **Panel** (HIGH criticality: worker + every mock site)

Rubric:
1. Completeness (0.35) — zero legacy `CFG` reads; `_update_pilot_tag` via `config.save()`; `_ffmpeg_path` delegated.
2. Live-reload correctness (0.25) — safe fields re-read at job start; locked constants import-time only.
3. Test migration integrity (0.25) — no remaining `addon.main.CFG`/`F5_*` patch sites; no test reads the real `config.json`.
4. Non-regression (0.15) — 49 baseline tests + new tests all pass.

### Step 4 — QTabWidget restructure — **Single** (MEDIUM)

Rubric:
1. Behavioral preservation (0.40) — Overview tab behaves identically (refresh, recommended action, scan, current, pilot tag field).
2. Structure (0.30) — six tabs in design order; callbacks injected; `_run_callback` used everywhere.
3. Import safety & accessibility (0.30) — non-Anki import guard intact; palette-only styling; accessible names present.

### Step 5 — Settings tab — **Per-Item** (MEDIUM, one eval per AC G1.1–G1.8)

Rubric per item:
1. AC fidelity (0.50) — observable behavior matches the Given/When/Then exactly.
2. No-write-on-invalid (0.30) — file mtime/content unchanged on rejected save.
3. UI wiring (0.20) — errors inline; Revert restores last-saved values.

### Steps 6–8 — read-only tabs — **Single** each (LOW–MEDIUM)

Rubric (shared):
1. AC coverage (0.50) — G2.1 / G3.1+G3.2 / G4.1+G4.2 demonstrably met.
2. Read-only discipline (0.30) — no mutation controls beyond the specified actions.
3. Refresh correctness (0.20) — data updates on tab switch / after actions.

### Step 9 — Scope + G11 — **Panel** (HIGH criticality: bulk enqueue)

Rubric:
1. ImpactDialog correctness (0.30) — pure `estimate_runtime` unit-tested; count matches `find_notes`.
2. Enqueue integrity (0.30) — digest dedupe (G11.5); per-note failure → "N of M" (G11.4); Cancel enqueues nothing (G11.3).
3. Scope toggle (0.20) — atomic `pilot_only` save; Overview reflects change (G5.1).
4. Stub replacement (0.20) — `_dashboard_unavailable` paths wired to real tabs.

### Step 10 — Integration — **None** (manual smoke + suite)

Verification = full suite green + smoke checklist in Definition of Done.

### Verification summary

| Step | Level | Threshold | Evaluations |
|---|---|---|---|
| 1 | Panel | 4.0 | 1 panel × 4 criteria |
| 2 | Single | 4.0 | 1 |
| 3 | Panel | 4.0 | 1 panel × 4 criteria |
| 4 | Single | 4.0 | 1 |
| 5 | Per-Item | 4.0 | 8 (G1.1–G1.8) |
| 6–8 | Single | 4.0 | 3 |
| 9 | Panel | 4.0 | 1 panel × 4 criteria |
| 10 | None | — | manual smoke |
