# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
Fixed-size duplicate filter for received sequence numbers.

Remembers the newest sequence number seen plus one bit for each of the
previous ``size`` numbers (the same idea as the IPsec/DTLS replay window).
Memory stays constant however many messages arrive, and every lookup is O(1).
"""

from enum import Enum

from .seq import distance

DEFAULT_WINDOW = 1024


class Verdict(Enum):
    """Result of checking one sequence number."""

    NEW = 'new'
    DUPLICATE = 'duplicate'
    TOO_OLD = 'too_old'


class DedupWindow:
    """Track which recent sequence numbers have already been received."""

    def __init__(self, size=DEFAULT_WINDOW):
        self.size = size
        self._mask = (1 << size) - 1
        self.reset()

    def reset(self):
        """Forget everything, e.g. after the peer restarts."""
        self.latest = None
        # Bit i is set when (latest - i) has been seen.
        self._bits = 0

    def check_and_mark(self, seq):
        """Classify ``seq`` and remember it if it is new."""
        if self.latest is None:
            self.latest = seq
            self._bits = 1
            return Verdict.NEW

        ahead = distance(self.latest, seq)
        if ahead > 0:
            self._bits = ((self._bits << ahead) | 1) & self._mask
            self.latest = seq
            return Verdict.NEW

        age = -ahead
        if age >= self.size:
            return Verdict.TOO_OLD
        bit = 1 << age
        if self._bits & bit:
            return Verdict.DUPLICATE
        self._bits |= bit
        return Verdict.NEW
