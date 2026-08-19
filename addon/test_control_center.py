"""Unit tests for addon.control_center pure helpers (Qt-free)."""

import unittest

try:
    from . import control_center
except ImportError:  # pragma: no cover - direct execution
    from addon import control_center


class EstimateRuntimeTests(unittest.TestCase):
    """G11 — ImpactDialog math is pure and unit-tested."""

    def test_scales_linearly_with_note_count(self):
        self.assertEqual(control_center.estimate_runtime(10, 30.0), 300.0)

    def test_default_per_note_average(self):
        self.assertEqual(
            control_center.estimate_runtime(2),
            2 * control_center.DEFAULT_PER_NOTE_SECONDS,
        )

    def test_zero_and_negative_counts_clamp_to_zero(self):
        self.assertEqual(control_center.estimate_runtime(0), 0.0)
        self.assertEqual(control_center.estimate_runtime(-5), 0.0)


if __name__ == "__main__":
    unittest.main()
