# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""Tests for wrap-safe sequence numbers."""

import unittest

from roverlink.protocol.seq import distance, is_newer, next_seq, SEQ_MOD


class TestSeq(unittest.TestCase):

    def test_next_wraps_to_zero(self):
        self.assertEqual(next_seq(65534), 65535)
        self.assertEqual(next_seq(65535), 0)
        self.assertEqual(next_seq(0), 1)

    def test_increasing_across_wrap(self):
        chain = [65534, 65535, 0, 1]
        for older, newer in zip(chain, chain[1:]):
            self.assertTrue(is_newer(newer, older), (newer, older))
            self.assertFalse(is_newer(older, newer), (older, newer))

    def test_equal_is_not_newer(self):
        self.assertFalse(is_newer(7, 7))
        self.assertEqual(distance(7, 7), 0)

    def test_distance_across_wrap(self):
        self.assertEqual(distance(65535, 0), 1)
        self.assertEqual(distance(0, 65535), -1)
        self.assertEqual(distance(65530, 5), 11)
        self.assertEqual(distance(5, 65530), -11)

    def test_distance_range(self):
        self.assertEqual(distance(0, 32767), 32767)
        self.assertEqual(distance(0, 32768), -32768)

    def test_many_sends_stay_in_range(self):
        seq = 0
        for _ in range(200_000):
            previous = seq
            seq = next_seq(seq)
            self.assertTrue(0 <= seq < SEQ_MOD)
            self.assertTrue(is_newer(seq, previous))


if __name__ == '__main__':
    unittest.main()
