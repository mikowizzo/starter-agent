#!/usr/bin/env python3
"""Golden tests for reputation.py (Slice 5). Pure-math expectations first."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

from reputation import SourceScore, REPUTATION_FLOOR


class GoldenMath(unittest.TestCase):
    def test_cold_start_is_neutral(self):
        s = SourceScore("run_x", "run", alpha=0.0, beta=0.0, n_claims=3)
        self.assertEqual(s.reliability, 1.0)   # uniform prior -> neutral
        self.assertEqual(s.multiplier, 1.0)    # full voice, no privilege

    def test_perfect_record(self):
        s = SourceScore("run_x", "run", alpha=4.0, beta=0.0, n_claims=4)
        self.assertAlmostEqual(s.reliability, 1.0)
        self.assertAlmostEqual(s.multiplier, 1.0)

    def test_even_split(self):
        # r = 0.5, m = floor + 0.5*(1-floor) = 0.10 + 0.45 = 0.55
        s = SourceScore("run_x", "run", alpha=2.0, beta=2.0, n_claims=4)
        self.assertAlmostEqual(s.reliability, 0.5)
        self.assertAlmostEqual(s.multiplier, REPUTATION_FLOOR + 0.5 * (1 - REPUTATION_FLOOR))
        self.assertAlmostEqual(s.multiplier, 0.55)

    def test_bad_source_hits_floor_not_zero(self):
        s = SourceScore("run_x", "run", alpha=0.0, beta=9.0, n_claims=2)
        self.assertAlmostEqual(s.reliability, 0.0)
        self.assertAlmostEqual(s.multiplier, REPUTATION_FLOOR)
        self.assertGreater(s.multiplier, 0.0)  # never silenced entirely

    def test_one_retraction_of_many(self):
        # 3 corroborations, 1 scored retraction -> r = 0.75
        s = SourceScore("run_x", "run", alpha=3.0, beta=1.0, n_claims=6)
        self.assertAlmostEqual(s.reliability, 0.75)
        self.assertAlmostEqual(s.multiplier, 0.10 + 0.75 * 0.90)

    def test_monotone_more_alpha_increases_multiplier(self):
        a3 = SourceScore("x", "run", 3.0, 1.0, 5).multiplier
        a4 = SourceScore("x", "run", 4.0, 1.0, 5).multiplier
        self.assertGreater(a4, a3)

    def test_monotone_more_beta_decreases_multiplier(self):
        b1 = SourceScore("x", "run", 2.0, 1.0, 5).multiplier
        b2 = SourceScore("x", "run", 2.0, 2.0, 5).multiplier
        self.assertGreater(b1, b2)

    def test_multiplier_bounds(self):
        for alpha in (0.0, 1.0, 5.0, 100.0):
            for beta in (0.0, 1.0, 5.0, 100.0):
                if alpha == 0 and beta == 0:
                    continue
                m = SourceScore("x", "run", alpha, beta, 1).multiplier
                self.assertGreaterEqual(m, REPUTATION_FLOOR)
                self.assertLessEqual(m, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
