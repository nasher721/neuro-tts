# Neuro ICU TTS — Config Center Design

## Context

The add-on writes engine settings to `config.json` and requires manual file edits for every change. This design adds a Config Center to the Control Center dialog: a versioned config schema, safe editable fields, and a full-deck synthesis action. The design covers only the unimplemented gaps G1–G5, G8, G11 from the UI-overhaul spec.

## Locked decisions

- **Q1 — Control Center depth**: implement Cluster A (G1, G2, G3, G4, G5, G8, G11) inside the Control Center.
- **Q2 — Locked engine, safe knobs**: keep the F5 model, repo, and reference audio fixed. Expose only non-destructive knobs: speed, device, ffmpeg path. Version the config schema, validate input, and live-reload safe fields.
- **Q3 — Core MVP**: fully design G1, G8, G11. Design G2–G5 as lighter read-only views and stubs.

## Approach

New pure `addon/config.py` module (versioned schema, getter layer, atomic writes). The Control Center gains a `QTabWidget`. The Control Center stays a renderer; app callbacks own side effects.

## 1. Architecture

Put the seam at config, not UI. `addon/config.py` owns schema version, defaults, validation, migration, atomic writes, and getters: `get(key)`, `get_speed()`, `get_device()`, `get_ffmpeg_path()`. `main.py` replaces direct `CFG` reads with these accessors.

`reload_config_if_changed() -> bool` compares mtime, then re-reads and validates. No watcher or diff. Reload triggers: the Control Center refresh cycle and the explicit Reload button.

The Control Center becomes a tabbed dialog:

- Overview — existing dashboard, stays first.
- Settings — full G1 form.
- Queue — G2 lighter view: counts + read-only JobStore rows.
- Diagnostics — G3 lighter view: engine test + log tail.
- Maintenance — G4 lighter view: clear finished jobs + storage size.
- Scope — G5 lighter view + G11 anchor: pilot-only toggle + full-deck button behind confirmation.

G11 fan-out reuses JobStore. No new queue machinery.

## 2. Components

`config.py` is pure — no Qt.

- `SCHEMA_VERSION = 2` (v1 is today's flat `config.json`).
- `DEFAULTS` — single source: `pilot_only`, `pilot_tag`, `speed`, `device`, `ffmpeg_path`; locked read-only engine keys `f5_tts_repo`, `f5_python`, `f5_ref_audio`, `f5_model`.
- `validate(raw) -> list[str]` — `pilot_tag` non-empty with no whitespace; `speed` in [0.5, 2.0]; `device` in {"cpu", "mps", "cuda"}; `ffmpeg_path` exists or empty.
- `migrate(raw)` — v1→v2 fills missing keys from defaults, preserves unknown keys.
- `load()` — read, migrate, validate. Falls back to defaults with warnings.
- `save(dict)` — atomic write via tmp + `os.replace`.
- `reload_config_if_changed() -> bool`.

Settings tab: `QFormLayout` with editable fields, locked engine fields, and Save/Revert/Reload buttons.

`ImpactDialog`: counts notes via `col.find_notes("deck:...")`, estimates runtime as len(notes) × per-note average, offers Confirm/Cancel.

Tests live in `test_config.py`.

## 3. Data flow

Read path: `load()` at import → getters → derived constants unchanged. Control Center Overview refreshes from `status_service.snapshot()`. Reload (refresh cycle or Reload button) re-loads, updates in-memory state, returns True. The worker re-reads safe fields (speed, device, ffmpeg_path) at the next job start — running jobs never interrupted.

Write path: Save → `config.save(dict)` atomic → inline validation errors (no save on error) → getters re-read → Control Center refresh. `_update_pilot_tag` reuses `save()`.

G11: Scope tab → Full-deck → ImpactDialog → Confirm → one job per note enqueued via existing JobStore → progress on Queue tab.

Diagnostics: same snapshot; engine test via existing callback; log tail = last N lines.

## 4. Error handling

`config.py` never raises at import. Read/JSON errors merge defaults and log warnings via `_configure_logging()`. Save refuses invalid values with inline errors (reuse the pilot-tag tooltip pattern). Invalid reload falls back to defaults — never crashes the worker. `reload_config_if_changed()` swallows transient read failures (returns False, keeps last-good).

Engine: worker wraps jobs; failures land in JobStore with error text shown on the Queue tab. MPS→cpu retry and ffmpeg fallback stay as-is. Engine test failure shows via banner.

G11: ImpactDialog failure → Cancel + banner, never partial enqueue. Mid-loop failure → banner "N of M notes queued". JobStore dedupes by digest.

UI: every tab callback goes through the existing `_run_callback` (disable → run → False/exception → banner → re-enable → refresh).

## 5. Testing

`test_config.py`:

- validation rejects bad `pilot_tag`, `speed`, `device`, `ffmpeg_path`;
- migration fills defaults and preserves unknown keys;
- `load()` falls back on corrupt JSON;
- reload returns True on mtime change, False on unchanged and on transient failure;
- atomic save writes valid JSON.

Existing tests keep passing; switch `CFG` assertions to getters (mechanical rename). Control Center is Qt-bound — no unit tests; smoke test: open Control Center → tabs render → Save persists → Reload picks up an external edit. G11: callback-level test with fake `col.find_notes` → N jobs enqueued; ImpactDialog math extracted as a pure unit-tested function.

Edge cases: empty or missing file, unknown future keys, speed bounds 0.5/2.0, same-second-write mtime limitation (documented), concurrent Save last-writer-wins.
