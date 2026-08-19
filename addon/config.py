"""Versioned, validated, auto-reloading configuration for the Neuro ICU TTS add-on.

Public API
----------
get(key)                 – read a single setting (falls back to DEFAULTS)
get_speed()              – f5_speed as *str* (matches main.py CLI expectations)
get_device()             – f5_device value
get_ffmpeg_path()        – resolved ffmpeg path or None
migrate(raw)             – fill defaults, stamp schema version, preserve unknown keys
validate(raw)            – list of human-readable validation errors (empty = valid)
load()                   – read + migrate + validate-warn config.json into module state
save(updates)            – validate + atomic write of editable keys; returns error list
reload_config_if_changed – mtime-guarded reload; returns True on change

Internal
--------
_read_raw()             – low-level file read, reports corrupt vs missing
_save(persisted)        – atomic write to disk (write-tmp-rename)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("neuroicu_tts")

# ── schema ──────────────────────────────────────────────────────────────────
SCHEMA_VERSION = 2

DEFAULTS: dict[str, Any] = {
    "f5_tts_repo": "",
    "f5_tts_python": "python3",
    "f5_model": "F5TTS_v1_Base",
    "f5_ref_audio": "",
    "f5_ref_text": "",
    "f5_device": "cpu",
    "f5_nfe_step": 16,
    "f5_speed": 1.0,
    "ffmpeg_path": "",
    "pilot_only": True,
    "pilot_tag": "neuroicu-tts-pilot",
}

# Keys the user may edit at runtime.  Everything else is engine-locked:
# save() silently ignores keys outside this set (and unknown keys).
EDITABLE_KEYS = frozenset({"f5_speed", "f5_device", "ffmpeg_path", "pilot_only", "pilot_tag"})

_DEVICE_CHOICES = {"cpu", "mps", "cuda"}
_SPEED_MIN, _SPEED_MAX = 0.5, 2.0

# ── paths ───────────────────────────────────────────────────────────────────
CONFIG_DIR = Path(__file__).resolve().parent
CONFIG_PATH = CONFIG_DIR / "config.json"

# ── module state ────────────────────────────────────────────────────────────
_cfg: dict[str, Any] = {}
_cfg_mtime: float = 0.0


# ── migration & validation ──────────────────────────────────────────────────
def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Return *raw* upgraded to the current schema.

    Missing keys are filled from DEFAULTS, the schema version is stamped, and
    unknown keys are preserved (forward compatibility with newer add-on versions).
    """
    merged = {**DEFAULTS, **raw}
    merged["schema_version"] = SCHEMA_VERSION
    return merged


def validate(raw: dict[str, Any]) -> list[str]:
    """Return a list of validation errors for *raw* (empty list means valid)."""
    errors: list[str] = []

    speed = raw.get("f5_speed", DEFAULTS["f5_speed"])
    try:
        speed_value = float(speed)
        if not _SPEED_MIN <= speed_value <= _SPEED_MAX:
            errors.append(f"speed must be between {_SPEED_MIN} and {_SPEED_MAX}")
    except (TypeError, ValueError):
        errors.append(f"speed must be between {_SPEED_MIN} and {_SPEED_MAX}")

    device = str(raw.get("f5_device", DEFAULTS["f5_device"])).strip().lower()
    if device not in _DEVICE_CHOICES:
        errors.append("device must be cpu, mps, or cuda")

    ffmpeg = raw.get("ffmpeg_path") or ""
    if ffmpeg and not Path(str(ffmpeg)).is_file():
        errors.append(f"ffmpeg_path does not exist: {ffmpeg}")

    tag = raw.get("pilot_tag", DEFAULTS["pilot_tag"])
    if not isinstance(tag, str) or not tag or any(character.isspace() for character in tag):
        errors.append("pilot_tag must be non-empty with no whitespace")

    return errors


# ── low-level I/O ──────────────────────────────────────────────────────────
def _read_raw() -> tuple[dict[str, Any], bool]:
    """Read config.json; return (raw dict, ok).

    *ok* is False only when the file exists but cannot be parsed — a missing
    file is a normal first-run condition and returns ({}, True).
    """
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, True
    except (OSError, json.JSONDecodeError, TypeError):
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def _save(persisted: dict[str, Any]) -> None:
    """Atomic write: write to .tmp, then rename into place."""
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise OSError(f"Could not write config.json: {exc}") from exc


# ── load / reload ───────────────────────────────────────────────────────────
def load() -> dict[str, Any]:
    """Read, migrate, and cache config.json; never raises on bad input."""
    global _cfg, _cfg_mtime
    raw, ok = _read_raw()
    if not ok:
        LOGGER.warning("config.json is corrupt or unreadable; falling back to defaults")
    merged = migrate(raw)
    for error in validate(merged):
        LOGGER.warning("config validation: %s", error)
    _cfg = merged
    try:
        _cfg_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        _cfg_mtime = 0.0
    return _cfg


def reload_config_if_changed() -> bool:
    """Re-read config.json when its mtime differs from the cached value.

    Returns *True* if the config was actually reloaded.  Transient read
    failures are swallowed and the last-good config is kept.
    """
    try:
        current_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return False
    if current_mtime == _cfg_mtime:
        return False
    load()
    LOGGER.info("config.json reloaded (mtime changed)")
    return True


# ── initialisation ──────────────────────────────────────────────────────────
load()


# ── public API ──────────────────────────────────────────────────────────────
def get(key: str, default: Any = None) -> Any:
    """Return *key* from the live config, falling back to DEFAULTS then *default*."""
    if key in _cfg:
        return _cfg[key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default


def get_speed() -> str:
    """f5_speed as string – main.py passes it to the CLI as ``--speed``."""
    return str(_cfg.get("f5_speed", DEFAULTS["f5_speed"]))


def get_device() -> str:
    return str(_cfg.get("f5_device", DEFAULTS["f5_device"]))


def get_ffmpeg_path() -> str | None:
    """Resolve ffmpeg: config → PATH → hard-coded candidates."""
    configured = _cfg.get("ffmpeg_path")
    candidates = ([configured] if configured else []) + [shutil.which("ffmpeg")] + [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def save(updates: dict[str, Any]) -> list[str]:
    """Validate and atomically persist editable *updates*; return error list.

    Keys outside EDITABLE_KEYS (locked engine keys, unknown keys) are ignored.
    On validation failure nothing is written and the errors are returned.
    An empty return list means the update was persisted.
    """
    accepted = {k: v for k, v in updates.items() if k in EDITABLE_KEYS}
    rejected = sorted(set(updates) - set(accepted))
    if rejected:
        LOGGER.warning("Ignoring non-editable config keys: %s", ", ".join(rejected))
    if not accepted:
        return []
    merged = {**_cfg, **accepted}
    merged["schema_version"] = SCHEMA_VERSION
    errors = validate(merged)
    if errors:
        LOGGER.warning("config save rejected: %s", "; ".join(errors))
        return errors
    _save(merged)
    _cfg.clear()
    _cfg.update(merged)
    global _cfg_mtime
    try:
        _cfg_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        pass
    LOGGER.info("config.json saved (%d keys)", len(merged))
    return []


def all_keys() -> list[str]:
    """Return the ordered list of known config keys (for the Settings tab)."""
    return list(DEFAULTS.keys())


def as_dict() -> dict[str, Any]:
    """Snapshot of the live config (copy – safe to mutate)."""
    return dict(_cfg)
