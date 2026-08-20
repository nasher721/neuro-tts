"""Guard the invariant that the shipped add-on is built, never hand-edited.

``addon/`` is the single source tree; ``neuroicu_tts_addon/`` is generated from
it by ``tools/package.py``.  Before this guard existed the two directories were
byte-identical copies kept in sync by hand, which silently shipped stale code
whenever only one side was edited.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "package.py"

sys.path.insert(0, str(ROOT / "tools"))
import package  # noqa: E402  (path is set up immediately above)


class PackageParityTests(unittest.TestCase):
    def test_generated_package_matches_the_source_tree(self):
        stale = package.stale_files()
        self.assertEqual(
            stale,
            [],
            "neuroicu_tts_addon/ is out of date; run: python3 tools/package.py",
        )

    def test_check_mode_exits_zero_when_current(self):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--check"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tests_are_never_shipped_to_users(self):
        shipped = {path.name for path in package.shipped_files()}
        self.assertFalse([name for name in shipped if name.startswith("test_")])
        self.assertIn("main.py", shipped)
        self.assertIn("config.json", shipped)
        self.assertIn("meta.json", shipped)

    def test_shipped_config_is_valid_and_free_of_machine_local_paths(self):
        try:
            from . import config
        except ImportError:  # pragma: no cover - supports PYTHONPATH=addon discovery
            from addon import config

        shipped = json.loads((ROOT / "addon" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config.validate(config.migrate(shipped)), [])
        for key in ("f5_tts_repo", "f5_ref_audio", "ffmpeg_path"):
            self.assertEqual(shipped.get(key, ""), "", f"{key} must ship empty")

    def test_shipped_config_declares_every_known_key(self):
        try:
            from . import config
        except ImportError:  # pragma: no cover
            from addon import config

        shipped = json.loads((ROOT / "addon" / "config.json").read_text(encoding="utf-8"))
        missing = sorted(set(config.DEFAULTS) - set(shipped))
        self.assertEqual(missing, [], "config.json is missing documented defaults")


if __name__ == "__main__":
    unittest.main()
