"""Unit tests for addon.config — G8 validation/migration, atomic save, reload."""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from . import config
except ImportError:  # pragma: no cover - direct execution
    from addon import config


class ConfigTestCase(unittest.TestCase):
    """Isolates the config module onto a temporary config.json per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self._orig_path = config.CONFIG_PATH
        self._orig_cfg = dict(config._cfg)
        self._orig_mtime = config._cfg_mtime
        config.CONFIG_PATH = self.path
        config.load()

    def tearDown(self):
        config.CONFIG_PATH = self._orig_path
        config._cfg.clear()
        config._cfg.update(self._orig_cfg)
        config._cfg_mtime = self._orig_mtime
        self._tmp.cleanup()

    # ── helpers ─────────────────────────────────────────────────────────────
    def write_raw(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_disk(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def bump_mtime(self) -> None:
        """Ensure a different mtime even within the same filesystem tick."""
        stat = self.path.stat()
        os.utime(self.path, (stat.st_atime, stat.st_mtime + 2))


class MigrationTests(ConfigTestCase):
    def test_migrate_fills_defaults_stamps_version_preserves_unknown(self):
        # G8.1 — a flat legacy config gains defaults + the current schema version,
        # and unknown keys are preserved for forward compatibility.
        raw = {"f5_speed": 1.5, "custom_future_key": {"nested": True}}
        migrated = config.migrate(raw)
        self.assertEqual(migrated["schema_version"], config.SCHEMA_VERSION)
        self.assertEqual(migrated["f5_speed"], 1.5)
        self.assertEqual(migrated["custom_future_key"], {"nested": True})
        for key in config.DEFAULTS:
            self.assertIn(key, migrated)

    def test_load_upgrades_v1_file_in_place(self):
        self.write_raw({"f5_speed": 1.25, "unknown": "kept"})
        config.load()
        self.assertEqual(config.get("schema_version"), config.SCHEMA_VERSION)
        self.assertEqual(config.get("unknown"), "kept")
        self.assertEqual(config.get_speed(), "1.25")

    def test_load_corrupt_json_falls_back_to_defaults_with_warning(self):
        # G8.2 — corrupt file must never raise; defaults apply; warning logged.
        self.path.write_text("{not valid json", encoding="utf-8")
        with self.assertLogs("neuroicu_tts", level="WARNING") as captured:
            config.load()
        self.assertEqual(config.get_speed(), str(config.DEFAULTS["f5_speed"]))
        self.assertTrue(any("corrupt" in line for line in captured.output))

    def test_load_missing_file_is_silent_first_run(self):
        config.load()  # setUp already removed nothing; file simply absent
        self.assertEqual(config.get_speed(), str(config.DEFAULTS["f5_speed"]))


class ValidationTests(ConfigTestCase):
    def test_validate_valid_config_returns_empty(self):
        # G8.3
        self.assertEqual(config.validate(config.migrate({})), [])

    def test_validate_reports_each_violation_distinctly(self):
        # G8.4 — out-of-range speed + whitespace pilot_tag → two distinct errors.
        errors = config.validate(config.migrate({"f5_speed": 5.0, "pilot_tag": "my tag"}))
        self.assertEqual(len(errors), 2)
        self.assertIn("speed must be between 0.5 and 2.0", errors)
        self.assertIn("pilot_tag must be non-empty with no whitespace", errors)

    def test_validate_speed_boundaries(self):
        for ok_value in (0.5, 2.0, 1.0, "1.5"):
            self.assertEqual(config.validate(config.migrate({"f5_speed": ok_value})), [])
        for bad_value in (0.49, 2.01, "fast"):
            errors = config.validate(config.migrate({"f5_speed": bad_value}))
            self.assertIn("speed must be between 0.5 and 2.0", errors)

    def test_validate_device_choices(self):
        for ok_device in ("cpu", "mps", "cuda"):
            self.assertEqual(config.validate(config.migrate({"f5_device": ok_device})), [])
        errors = config.validate(config.migrate({"f5_device": "tpu"}))
        self.assertIn("device must be cpu, mps, or cuda", errors)

    def test_validate_ffmpeg_path(self):
        self.assertEqual(config.validate(config.migrate({"ffmpeg_path": ""})), [])
        existing = Path(self._tmp.name) / "ffmpeg"
        existing.touch()
        self.assertEqual(config.validate(config.migrate({"ffmpeg_path": str(existing)})), [])
        errors = config.validate(config.migrate({"ffmpeg_path": "/no/such/ffmpeg"}))
        self.assertTrue(any(e.startswith("ffmpeg_path does not exist") for e in errors))

    def test_validate_pilot_tag(self):
        for bad_tag in ("", "has space", "trailing ", "tab\ttag"):
            errors = config.validate(config.migrate({"pilot_tag": bad_tag}))
            self.assertIn("pilot_tag must be non-empty with no whitespace", errors)


class SaveTests(ConfigTestCase):
    def test_save_valid_update_persists_atomically(self):
        # G1.2 — save writes valid JSON; getter reflects the new value.
        errors = config.save({"f5_speed": 1.5})
        self.assertEqual(errors, [])
        on_disk = self.read_disk()
        self.assertEqual(on_disk["f5_speed"], 1.5)
        self.assertEqual(on_disk["schema_version"], config.SCHEMA_VERSION)
        self.assertEqual(config.get_speed(), "1.5")
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_save_invalid_writes_nothing(self):
        # G1.5 — invalid speed: errors returned, file untouched.
        self.write_raw({"f5_speed": 1.0})
        config.load()
        before = self.path.read_text(encoding="utf-8")
        errors = config.save({"f5_speed": 5.0})
        self.assertIn("speed must be between 0.5 and 2.0", errors)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_save_persists_engine_keys_and_ignores_unknown_keys(self):
        # Engine settings became Control Center editable in schema 3; unknown
        # keys are still dropped rather than written back.
        self.write_raw({"f5_model": "F5TTS_v1_Base", "f5_speed": 1.0})
        config.load()
        self.assertEqual(config.save({"f5_model": "OtherModel", "unknown_key": 1}), [])
        self.assertEqual(config.get("f5_model"), "OtherModel")
        self.assertEqual(self.read_disk()["f5_model"], "OtherModel")
        self.assertNotIn("unknown_key", self.read_disk())

    def test_save_rejects_an_empty_model_name(self):
        self.write_raw({"f5_speed": 1.0})
        config.load()
        before = self.path.read_text(encoding="utf-8")
        self.assertIn("f5_model must not be empty", config.save({"f5_model": "  "}))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_save_preserves_unknown_keys_already_loaded(self):
        self.write_raw({"future_key": "preserve-me", "f5_speed": 1.0})
        config.load()
        self.assertEqual(config.save({"f5_speed": 1.5}), [])
        self.assertEqual(self.read_disk()["future_key"], "preserve-me")


class ReloadTests(ConfigTestCase):
    def test_reload_returns_false_when_unchanged(self):
        self.write_raw({"f5_speed": 1.0})
        config.load()
        self.assertFalse(config.reload_config_if_changed())

    def test_reload_picks_up_external_edit(self):
        # G1.4 backbone — external edit + mtime change → reload returns True.
        self.write_raw({"f5_speed": 1.0})
        config.load()
        self.write_raw({"f5_speed": 1.75})
        self.bump_mtime()
        self.assertTrue(config.reload_config_if_changed())
        self.assertEqual(config.get_speed(), "1.75")

    def test_reload_missing_file_returns_false_keeps_last_good(self):
        self.write_raw({"f5_speed": 1.5})
        config.load()
        self.path.unlink()
        self.assertFalse(config.reload_config_if_changed())
        self.assertEqual(config.get_speed(), "1.5")


if __name__ == "__main__":
    unittest.main()
