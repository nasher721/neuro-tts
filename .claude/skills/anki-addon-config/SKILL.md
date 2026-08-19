---
name: anki-addon-config
description: Patterns for versioned, validated, atomically-saved configuration in Anki add-ons — pure config modules, mtime reload, QTabWidget Control Centers, and safe UI callbacks. Use when adding settings UIs or config persistence to Anki add-ons.
---

# Anki Add-on Config & Control Center Patterns

Reusable patterns for Anki add-ons that need user-editable configuration without hand-editing `config.json`.

## 1. Pure config module (no Qt, no aqt)

Keep the config layer importable without Anki so unit tests run with plain `python3 -m unittest`:

- One module (`config.py`) owns: `SCHEMA_VERSION`, `DEFAULTS`, `validate(raw) -> list[str]`, `migrate(raw)`, `load()`, `save(updates)`, `reload_config_if_changed() -> bool`, typed getters (`get_speed()`, `get_device()`, …).
- Never raise at import: read/JSON errors merge `DEFAULTS` and log a warning.
- `migrate()` fills missing keys from `DEFAULTS`, stamps the schema version, and **preserves unknown keys** (forward compatibility).
- `save()` is atomic: write `config.json.tmp`, then `os.replace()`. Refuse invalid updates (validate first, return errors, write nothing).
- Guard edits with an `EDITABLE_KEYS` frozenset; locked engine keys are read-only in UI and rejected by `save()`.
- Reload via mtime comparison only (no file watchers); swallow transient read failures and keep last-good config.

## 2. Eliminating duplicate config sources

A common add-on failure mode is two parallel config readers (module-level `CFG = json.loads(...)` plus a newer config module). Rules:

- Exactly one module reads the file. Everything else imports getters.
- Replace module-level constants (`SPEED = str(CFG.get(...))`) with function calls at point of use, so live-reload actually reaches workers.
- Long-running worker threads re-read *safe* fields (speed, device, paths) at job start; never interrupt a running job.
- When migrating tests: mocks move from `pkg.main.CFG` to `pkg.config.<getter>` — search for every `@patch` site systematically before renaming.

## 3. QTabWidget Control Center

- The dialog stays a **renderer**; all side effects live in injected callbacks (`on_save(dict) -> list[str]`, `on_engine_test()`, `on_clear_finished() -> int`, …). This keeps Qt code untested-but-thin and logic unit-testable.
- Every tab callback goes through one `_run_callback` wrapper: disable buttons → run → on `False`/exception show banner → re-enable → refresh.
- Use palette-based styles (`palette(mid)`), never fixed colors — Anki dark mode and high-contrast themes must survive.
- Set `setAccessibleName` on every interactive widget; Anki users rely on screen readers.

## 4. Fan-out batch actions (full-deck conversion)

- Put the action behind an ImpactDialog: note count + estimated runtime (pure function `estimate_runtime(n, per_note_seconds)` — unit-test the math).
- Confirm → enqueue one job per note via the existing JobStore; Cancel → nothing enqueued.
- Dedupe by content digest, not note id (re-runs must be idempotent).
- Mid-loop failure → report "N of M queued"; never leave partial inconsistent state.

## 5. Testing

- Pure modules (config, job store, estimators): full unit tests with `unittest`, no Anki needed.
- Qt dialogs: smoke test manually (open dialog → tabs render → save persists → reload picks up external edit).
- Baseline command in this repo: `PYTHONPATH=addon python3 -m unittest discover -s addon -v`.
