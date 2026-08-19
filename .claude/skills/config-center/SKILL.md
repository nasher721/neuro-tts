---
name: "config-center"
description: "Knowledge and patterns for implementing the Config Center feature — a Control Center with QTabWidget tabs for settings management, queue monitoring, diagnostics, and maintenance in the NeuroICU TTS Add-on."
---

# Config Center Skill

## Overview

This skill captures research knowledge for implementing the **"Implement Config Center"** feature in the NeuroICU TTS Add-on for Anki. The feature transforms the existing `control_center.py` Control Center into a tabbed interface (QTabWidget) with sections for Overview, Settings, Queue, Diagnostics, and Maintenance.

**Scope**: Core MVP only (G1: QTabWidget tabs, G8: Settings tab with config.json editing, G11: Engine restart flow). Other groups (G2-G5) are lighter read-only tabs.

**Decision Lock**: Control Center with QTabWidget; engine locked (only speed/device/ffmpeg_path editable); Core MVP scope.

---

## Key Resources

| File | Purpose | Status |
|------|---------|--------|
| `specs/tasks/in-progress/neuroicu-tts-ui-overhaul.feature.md` | Source specification (full feature design) | ✅ Read |
| `.specs/tasks/draft/implement-config-center.feature.md` | Task file (scope, acceptance criteria) | ✅ Read |
| `.specs/plans/neuroicu-tts-config-center.design.md` | Ground truth design doc (versioned schema, atomic writes, getter layer, mtime live-reload, QTabWidget tabs) | ✅ Read |
| `addon/main.py` | Module-level CFG pattern, CONFIG_DIR, `_config()`, global config reads | ✅ Read |
| `addon/control_center.py` | Current QDialog + StatusService subscription | ✅ Read |
| `addon/status.py` | Pure dataclasses (EngineSnapshot, ScopeSnapshot, QueueSnapshot, OverviewViewModel) | ✅ Read |
| `addon/tts_core.py` | Pure functions, TTSState dataclass | ✅ Read |
| `addon/config.json` | Flat JSON (11 lines, no version field) | ✅ Read |

---

## Core Patterns

### 1. Versioned Config Schema

**Current State**: `config.json` is flat (11 lines, no version field).

**Target State**: Add `"version": 1` to schema. Migrations run on startup before any reads.

```json
{
  "version": 1,
  "anki_media": "/path/to/anki/media",
  "default_voice": "zh-CN-XiaoxiaoNeural",
  "default_rate": "+0%",
  "default_volume": "+0%",
  "ffmpeg_path": "/usr/bin/ffmpeg",
  "hl_patient_color": "#E74C3C",
  "hl_context_color": "#3498DB",
  "hl_inactive_opacity": 0.3,
  "speed": 1.0,
  "device": "cpu"
}
```

**Migration Pattern** (in `addon/config.py`):
```python
def migrate_config(data: dict) -> dict:
    """Run migrations in order. Mutates and returns data."""
    if "version" not in data:
        data = _migrate_0_to_1(data)
    # Future: if data["version"] == 1: data = _migrate_1_to_2(data)
    return data

def _migrate_0_to_1(data: dict) -> dict:
    """Add version field, set defaults."""
    data["version"] = 1
    # Set defaults for any missing keys
    for key, default in DEFAULTS.items():
        if key not in data:
            data[key] = default
    return data
```

**Reusability**: This pattern scales to any future schema changes. Always add a version bump in the same PR as the migration function.

---

### 2. Atomic Writes (Crash-Safe)

**Pattern**: Write to temp file, then `os.replace()` to final path.

```python
import json
import os
import tempfile
from pathlib import Path

def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically. Crash-safe."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)  # Atomic on POSIX
    except Exception:
        os.unlink(tmp_path)  # Cleanup on failure
        raise
```

**Reusability**: Use this for ALL config writes. Never write directly to `config.json`.

---

### 3. Getter Layer (Read-Only Access)

**Pattern**: Expose config values via pure functions. No global `CFG` access from new code.

```python
# addon/config.py
from pathlib import Path
import json

CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"

def get_config() -> dict:
    """Load and return current config. Caller caches if needed."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_anki_media_path() -> str:
    return get_config().get("anki_media", "")

def get_speed() -> float:
    return get_config().get("speed", 1.0)

def get_device() -> str:
    return get_config().get("device", "cpu")
```

**Reusability**: New features should use getter functions, not access `CFG` directly. This centralizes config logic and makes testing easier.

---

### 4. Live-Reload via mtime Check

**Pattern**: Poll `config.json` modification time. Reload on change.

```python
import os
from pathlib import Path

class ConfigWatcher:
    def __init__(self, config_path: Path):
        self._path = config_path
        self._last_mtime = 0.0
        self._cached_config: dict | None = None
    
    def get_config(self) -> dict:
        """Return cached config if mtime unchanged, else reload."""
        current_mtime = os.path.getmtime(self._path)
        if current_mtime != self._last_mtime:
            self._cached_config = self._load()
            self._last_mtime = current_mtime
        return self._cached_config
    
    def _load(self) -> dict:
        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return migrate_config(data)  # Apply migrations
```

**Reusability**: Use in Control Center to detect external config changes. Poll on tab switch or periodic timer (e.g., every 5 seconds).

---

### 5. QTabWidget Control Center

**Current State**: `control_center.py` is a `QDialog` with a single layout.

**Target State**: `ControlCenter(QDialog)` with `QTabWidget` containing:
1. **Overview Tab** (G11): Status summary, queue snapshot
2. **Settings Tab** (G8): Edit speed, device, ffmpeg_path; save to config.json
3. **Queue Tab** (G1): Queue management, clear/pause/resume
4. **Diagnostics Tab** (G2): Read-only logs, engine status
5. **Maintenance Tab** (G3): Read-only cache info, logs location

**Pattern**:
```python
from PyQt5.QtWidgets import QDialog, QTabWidget, QVBoxLayout

class ControlCenter(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NeuroICU TTS Control Center")
        self.setMinimumSize(600, 400)
        
        layout = QVBoxLayout(self)
        
        # Tab widget
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        # Add tabs
        self.tabs.addTab(self._create_overview_tab(), "Overview")
        self.tabs.addTab(self._create_settings_tab(), "Settings")
        self.tabs.addTab(self._create_queue_tab(), "Queue")
        self.tabs.addTab(self._create_diagnostics_tab(), "Diagnostics")
        self.tabs.addTab(self._create_maintenance_tab(), "Maintenance")
    
    def _create_settings_tab(self) -> QWidget:
        """G8: Editable settings (speed, device, ffmpeg_path)."""
        # ... implement with QFormLayout, QDoubleSpinBox, QComboBox, QLineEdit
        pass
```

**Reusability**: This pattern extends to any dialog with multiple sections. Use `QTabWidget` for feature-rich UIs.

---

### 6. Module-Level CFG Pattern

**Current State**: `main.py` uses `CFG = _config()` at module level.

**Target State**: Migrate new code to use `config.py` getter functions. Keep `CFG` for backward compatibility during transition.

```python
# addon/main.py (existing)
def _config() -> dict:
    config_path = CONFIG_DIR / "config.json"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

CFG = _config()  # Module-level singleton
```

**Reusability**: New modules should import from `config.py` getters, not `main.CFG`. This avoids circular imports and centralizes config logic.

---

## Pitfalls & Gotchas

### 1. **No Circular Imports**
- `config.py` must NOT import from `main.py` or `control_center.py`
- `control_center.py` CAN import from `config.py` and `status.py`
- `main.py` CAN import from `config.py`

### 2. **Atomic Writes Are Mandatory**
- NEVER write directly to `config.json` — always use `atomic_write_json()`
- Reason: Anki may read config mid-write; atomic prevents corruption

### 3. **mtime Check Is Not Real-Time**
- Polling interval matters: too fast = CPU waste, too slow = stale config
- Recommended: check on tab switch + every 5 seconds while dialog is open

### 4. **Migration Functions Must Be Idempotent**
- Running `_migrate_0_to_1()` twice should not break config
- Use `"version" not in data` guard, not `"version" in data`

### 5. **QTabWidget Tabs Must Be QWidget Subclasses**
- Each tab is a `QWidget` with its own layout
- Do NOT put tabs in the same layout — each gets its own `QWidget`

### 6. **Settings Tab Must Validate Before Save**
- Speed: float, 0.1-3.0 range
- Device: enum ("cpu", "cuda", "auto")
- ffmpeg_path: exists on disk (optional validation)

### 7. **Engine Restart Is Async**
- G11 requires restarting engine after config change
- Use `QTimer.singleShot()` or signal to avoid blocking UI
- Show "Restarting..." status while engine restarts

---

## Reusable Knowledge

### File Organization
```
addon/
├── config.json          # Flat JSON, 11 lines, no version (current)
├── config.py            # NEW: getters, migrations, atomic_write_json
├── control_center.py    # MODIFY: QTabWidget tabs
├── main.py              # KEEP: module-level CFG (backward compat)
├── status.py            # READ-ONLY: pure dataclasses
└── tts_core.py          # READ-ONLY: pure functions
```

### Testing Patterns
- **Unit tests**: `config.py` functions (migrations, getters) — pure, no Qt
- **Integration tests**: `ControlCenter` tab switching — requires Qt test harness
- **Manual tests**: Settings save/reload, engine restart flow

### Acceptance Criteria (from task file)
- [ ] G1: QTabWidget with 5 tabs renders correctly
- [ ] G8: Settings tab edits speed/device/ffmpeg_path, saves to config.json
- [ ] G8: Atomic writes prevent config corruption
- [ ] G8: Getter layer provides read access
- [ ] G11: Engine restarts after settings change
- [ ] G11: Overview tab shows engine status + queue snapshot
- [ ] All tabs show correct data (read-only for G2-G5)

### Version History
- **v1**: Initial schema with version field, 11 config keys
- **Future**: Add more keys, migrate via `migrate_config()`
