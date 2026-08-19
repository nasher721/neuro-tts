# Config Center — Codebase Analysis (revised, current-state)

> **Feature**: Implement Config Center (sdd:plan Phase 2b, inline revision 2026-08-18)
> **Design doc**: `.specs/plans/neuroicu-tts-config-center.design.md`
> **Skill**: `.claude/skills/anki-addon-config/SKILL.md`
> **Baseline**: 49/49 tests pass (`PYTHONPATH=addon python3 -m unittest discover -s addon`)

---

## 1. Critical finding: two parallel config implementations exist

The earlier analysis assumed `addon/config.py` did not exist. **It does** (179 lines) — a partial first attempt at the design — but **nothing imports it**. `main.py` still runs entirely on its own legacy `_config()` / `CFG` / module-level constants. Reconciling these two implementations is the hidden core of this task.

| Aspect | Design doc requirement | Existing `addon/config.py` | Gap |
|---|---|---|---|
| Schema version | `SCHEMA_VERSION = 2` | `SCHEMA_VERSION = 1` | Bump to 2 |
| `validate(raw) -> list[str]` | Required (speed range, device set, ffmpeg exists, pilot_tag no whitespace) | **Missing entirely** | Add |
| `migrate(raw)` | Fill defaults, **preserve unknown keys** | `_ensure_versioned` only stamps version; `_load()` **drops unknown keys** (`{k: v ... if k in DEFAULTS}`) | Rewrite load/migrate |
| `save(updates)` | Validate first; refuse invalid; editable keys only | Accepts any key in `DEFAULTS`; no validation; `EDITABLE_KEYS` includes locked engine keys | Restrict to safe keys + validate |
| Editable keys | Only `speed`, `device`, `ffmpeg_path` (+ `pilot_only`, `pilot_tag` via Scope tab) | `EDITABLE_KEYS` lists engine keys too — and is never enforced | Redefine + enforce in `save()` |
| `reload_config_if_changed()` | mtime compare, swallow transient errors | Implemented correctly | Keep |
| `get_speed/get_device/get_ffmpeg_path` | Required getters | Present (`get_speed` returns `str`, matching CLI usage) | Keep |
| CONFIG_DIR | — | `Path(__file__).parent` (addon dir) — **same as `main.py`'s**, so no path conflict | Keep |

## 2. File inventory (current)

| File | Lines | Role | Change needed |
|---|---|---|---|
| `addon/config.py` | 179 | Unused partial config module | **Upgrade** to full design spec |
| `addon/main.py` | 795 | Entry point, JobStore, WorkerAndEngine, dashboard callbacks | **Migrate** off legacy `CFG`; extend JobStore; wire new tab callbacks |
| `addon/control_center.py` | 250 | Overview-only `QDialog` (no tabs) | **Restructure** into `QTabWidget` with 6 tabs |
| `addon/status.py` | 302 | StatusService + OverviewViewModel (pure) | Minor: reuse snapshots for Queue/Diagnostics tabs |
| `addon/tts_core.py` | 104 | Pure TTS helpers | None |
| `addon/test_main.py` | 540 | 49 tests; patches `addon.main.CFG`, `addon.main.F5_*` | **Migrate mocks** to `addon.config` getters |
| `addon/test_config.py` | — | Does not exist | **Create** (validation, migration, atomic save, reload) |
| `addon/config.json` | — | Live user config (flat, unversioned, no `ffmpeg_path` key) | Migrated transparently by `load()` |
| `addon/README.md` | — | Setup docs mention manual `config.json` edits | Update (post-MVP polish) |

## 3. `main.py` integration points (verified line numbers)

| Location | Code | Required change |
|---|---|---|
| L24–37 | `_config()`, `CFG = _config()`, `F5_TTS_REPO`…`PILOT_TAG` constants | Replace with `from . import config` + getters at point of use |
| L168–174 | `_ffmpeg_path()` duplicates `config.get_ffmpeg_path()` logic | Delegate to `config.get_ffmpeg_path()` |
| L178 | `_run_f5_inference` uses module constants `F5_DEVICE`, `F5_SPEED` | Re-read via `config.get_device()` / `config.get_speed()` at job start (live-reload of safe fields) |
| L591–621 | `_dashboard_action` / `_dashboard_unavailable` — stubs for "Open Voice & engine", "Open Queue", "Review failures", "Review scope" | Replace stubs with real tab-switch / action wiring |
| L624–646 | `_update_pilot_tag` does its own ad-hoc atomic write + `CFG.clear()/update()` | Reroute through `config.save({"pilot_tag": …})` |
| L648–651 | `_show_center` constructs `ControlCenter` | Pass new tab callbacks (settings save/revert/reload, engine test, clear finished, full-deck convert, scope toggle) |
| L112–130 | `JobStore` (sqlite) | Add `counts_by_status()`, `clear_finished()`, `existing_digests()` for G2/G4/G11 |

## 4. ControlCenter restructure points

- Existing Overview content (banner, refresh button, status cards, recommended action, advanced controls, details disclosure) becomes **Tab 1 (Overview)** unchanged in behavior.
- `_run_callback` (L212–227) is the containment wrapper — all new tab callbacks must go through it.
- Pilot-tag field in "Advanced controls" (L84–105) is superseded by the Scope tab; keep Overview behavior stable and move editing to Scope/Settings (decision: keep the existing field working during migration, remove once Scope tab lands).
- Qt-import guard pattern (`try: from aqt.qt import …`) must be preserved so pure tests keep running without Anki.

## 5. Test-mock migration surface

- `test_main.py` patches `addon.main.CFG` (~19 sites) and `addon.main.F5_*` (~14 sites).
- After migration these become `addon.config` getter patches or direct `config._cfg` fixture setup.
- One existing test asserts the failing-save path of `_update_pilot_tag` (`/path/that/does/not/exist/config.json.tmp`) — must be re-pointed at `config.save()` failure semantics.

## 6. Risk assessment

| Risk | Level | Mitigation |
|---|---|---|
| Breaking the worker by swapping constants to getters mid-run | High | Worker re-reads safe fields at **job start** only; locked engine constants captured once at import |
| `save()` rejecting previously-accepted keys breaks `_update_pilot_tag` | Medium | `pilot_tag` stays in `EDITABLE_KEYS`; single write path through `config.save()` |
| Mock migration misses a patch site → silent real-config reads in tests | Medium | Systematic grep for `addon.main.CFG` / `addon.main.F5_`; run full suite after each rename batch |
| mtime same-second writes make reload miss a change | Low | Documented edge case in design; acceptable |
| QTabWidget restructure regresses existing Overview behavior | Medium | Move code verbatim into tab; smoke test checklist (open → tabs render → save → reload) |
| G11 mid-loop enqueue failure | Medium | Per-note try/except, banner "N of M notes queued", digest dedupe via `existing_digests()` |

## 7. Out of scope (confirmed)

- `vendor/F5-TTS/` engine internals, `packages/` archives, `neuroicu_tts_addon/` (stale duplicate tree — do not touch), `neuroicu-tts-project-config-center/` (empty worktree dir).
- No new queue machinery; G11 reuses JobStore.
