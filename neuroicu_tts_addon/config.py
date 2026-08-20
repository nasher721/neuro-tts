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
SCHEMA_VERSION = 3

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
    # schema 3 — speech shaping, chunking, and queue reliability
    "normalize_speech": True,
    "expand_abbreviations": True,
    "speech_replacements": {},
    "max_chunk_chars": 800,
    "max_attempts": 3,
    "retry_backoff_seconds": 30.0,
    "log_level": "INFO",
}

# Keys the user may edit at runtime through the Control Center.  Engine paths
# became editable in schema 3; previously they required hand-editing this file.
# save() silently ignores keys outside this set (and unknown keys).
ENGINE_KEYS = frozenset(
    {"f5_tts_repo", "f5_tts_python", "f5_model", "f5_ref_audio", "f5_ref_text", "f5_nfe_step"}
)
EDITABLE_KEYS = frozenset(
    {
        "f5_speed",
        "f5_device",
        "ffmpeg_path",
        "pilot_only",
        "pilot_tag",
        "normalize_speech",
        "expand_abbreviations",
        "speech_replacements",
        "max_chunk_chars",
        "max_attempts",
        "retry_backoff_seconds",
        "log_level",
    }
) | ENGINE_KEYS

_DEVICE_CHOICES = {"cpu", "mps", "cuda"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_SPEED_MIN, _SPEED_MAX = 0.5, 2.0
_NFE_MIN, _NFE_MAX = 4, 128
_CHUNK_MIN, _CHUNK_MAX = 100, 5000
_ATTEMPTS_MIN, _ATTEMPTS_MAX = 1, 10

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

    errors.extend(_validate_engine(raw))
    errors.extend(_validate_speech(raw))
    return errors


def _validate_engine(raw: dict[str, Any]) -> list[str]:
    """Engine paths are optional until set, but must be usable once they are."""
    errors: list[str] = []

    repo = str(raw.get("f5_tts_repo", "") or "").strip()
    if repo:
        repo_path = Path(repo)
        if not repo_path.is_dir():
            errors.append(f"f5_tts_repo is not a directory: {repo}")
        elif not (repo_path / "src" / "f5_tts" / "infer" / "infer_cli.py").is_file():
            errors.append("f5_tts_repo does not contain src/f5_tts/infer/infer_cli.py")

    python = str(raw.get("f5_tts_python", "") or "").strip()
    if not python:
        errors.append("f5_tts_python must not be empty")
    elif os.sep in python and not (Path(python).is_file() and os.access(python, os.X_OK)):
        errors.append(f"f5_tts_python is not an executable file: {python}")
    # A bare command name is resolved against PATH at run time rather than here:
    # PATH inside a GUI launch differs from PATH in a shell, and blocking the
    # save would strand the user unable to edit any other setting.

    if not str(raw.get("f5_model", "") or "").strip():
        errors.append("f5_model must not be empty")

    reference = str(raw.get("f5_ref_audio", "") or "").strip()
    if reference:
        candidate = Path(reference)
        if not candidate.is_absolute() and repo:
            candidate = Path(repo) / reference
        if not candidate.is_file():
            errors.append(f"f5_ref_audio does not exist: {reference}")
        elif not str(raw.get("f5_ref_text", "") or "").strip():
            errors.append("f5_ref_text must transcribe f5_ref_audio exactly")

    try:
        nfe = int(raw.get("f5_nfe_step", DEFAULTS["f5_nfe_step"]))
        if not _NFE_MIN <= nfe <= _NFE_MAX:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"f5_nfe_step must be an integer between {_NFE_MIN} and {_NFE_MAX}")

    return errors


def _validate_speech(raw: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    replacements = raw.get("speech_replacements", {})
    if not isinstance(replacements, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in replacements.items()
    ):
        errors.append("speech_replacements must be an object of text->text pairs")

    for key, low, high in (
        ("max_chunk_chars", _CHUNK_MIN, _CHUNK_MAX),
        ("max_attempts", _ATTEMPTS_MIN, _ATTEMPTS_MAX),
    ):
        try:
            value = int(raw.get(key, DEFAULTS[key]))
            if not low <= value <= high:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{key} must be an integer between {low} and {high}")

    try:
        backoff = float(raw.get("retry_backoff_seconds", DEFAULTS["retry_backoff_seconds"]))
        if not 0 <= backoff <= 3600:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("retry_backoff_seconds must be a number between 0 and 3600")

    level = str(raw.get("log_level", DEFAULTS["log_level"])).strip().upper()
    if level not in _LOG_LEVELS:
        errors.append("log_level must be DEBUG, INFO, WARNING, or ERROR")

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
    return _cfg.get(key, DEFAULTS.get(key, default))


def get_speed() -> str:
    """f5_speed as string – main.py passes it to the CLI as ``--speed``."""
    return str(_cfg.get("f5_speed", DEFAULTS["f5_speed"]))


def get_device() -> str:
    return str(_cfg.get("f5_device", DEFAULTS["f5_device"]))


def get_int(key: str) -> int:
    """Return *key* coerced to int, falling back to its default when unusable."""
    try:
        return int(_cfg.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def get_float(key: str) -> float:
    try:
        return float(_cfg.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def get_bool(key: str) -> bool:
    return bool(_cfg.get(key, DEFAULTS[key]))


def get_speech_replacements() -> dict[str, str]:
    """User-supplied pronunciation overrides; invalid entries are dropped."""
    raw = _cfg.get("speech_replacements", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and k}


def speech_profile() -> dict[str, Any]:
    """The subset of settings that changes generated audio without changing text."""
    return {
        "normalize_speech": get_bool("normalize_speech"),
        "expand_abbreviations": get_bool("expand_abbreviations"),
        "speech_replacements": dict(sorted(get_speech_replacements().items())),
        "max_chunk_chars": get_int("max_chunk_chars"),
    }


def get_ffmpeg_path() -> str | None:
    """Resolve ffmpeg: config → PATH → hard-coded candidates."""
    configured = _cfg.get("ffmpeg_path")
    if configured and os.path.isfile(str(configured)) and os.access(configured, os.X_OK):
        return str(configured)
    candidates = (
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
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
