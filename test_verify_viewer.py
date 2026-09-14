#!/usr/bin/env python3
"""Tests for viewer verification rate accounting."""

from __future__ import annotations

import unittest

from verify_viewer import minimum_frames_for_sample


class ViewerRateAccountingTest(unittest.TestCase):
    def test_twelve_fps_for_three_seconds_requires_36_frames(self) -> None:
        self.assertEqual(minimum_frames_for_sample(12.0, 3.0), 36)

    def test_fractional_requirement_rounds_up(self) -> None:
        self.assertEqual(minimum_frames_for_sample(11.5, 3.0), 35)


if __name__ == "__main__":
    unittest.main()
