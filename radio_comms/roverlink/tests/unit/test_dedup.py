# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""Tests for the duplicate filter window."""

import unittest

from roverlink.protocol.dedup import DedupWindow, Verdict
from roverlink.protocol.seq import next_seq


class TestDedupWindow(unittest.TestCase):

    def test_first_is_new(self):
        self.assertEqual(DedupWindow().check_and_mark(500), Verdict.NEW)

    def test_duplicate(self):
        window = DedupWindow()
        window.check_and_mark(10)
        self.assertEqual(window.check_and_mark(10), Verdict.DUPLICATE)

    def test_out_of_order_inside_window(self):
        window = DedupWindow(size=16)
        for seq in (1, 5, 3, 2, 4):
            self.assertEqual(window.check_and_mark(seq), Verdict.NEW, seq)
        for seq in (1, 2, 3, 4, 5):
            self.assertEqual(window.check_and_mark(seq), Verdict.DUPLICATE, seq)

    def test_too_old(self):
        window = DedupWindow(size=16)
        window.check_and_mark(100)
        self.assertEqual(window.check_and_mark(85), Verdict.NEW)  # age 15
        self.assertEqual(window.check_and_mark(84), Verdict.TOO_OLD)  # age 16

    def test_big_jump_forgets_old_bits(self):
        window = DedupWindow(size=16)
        window.check_and_mark(1)
        window.check_and_mark(1000)
        self.assertEqual(window.check_and_mark(999), Verdict.NEW)

    def test_across_wrap(self):
        window = DedupWindow(size=16)
        for seq in (65534, 65535, 0, 1):
            self.assertEqual(window.check_and_mark(seq), Verdict.NEW, seq)
        self.assertEqual(window.check_and_mark(65535), Verdict.DUPLICATE)
        self.assertEqual(window.latest, 1)

    def test_reset(self):
        window = DedupWindow()
        window.check_and_mark(10)
        window.reset()
        self.assertEqual(window.check_and_mark(10), Verdict.NEW)

    def test_bounded_after_many(self):
        window = DedupWindow(size=1024)
        seq = 0
        for _ in range(100_000):
            self.assertEqual(window.check_and_mark(seq), Verdict.NEW)
            seq = next_seq(seq)
        self.assertLess(window._bits.bit_length(), window.size + 1)


if __name__ == '__main__':
    unittest.main()
