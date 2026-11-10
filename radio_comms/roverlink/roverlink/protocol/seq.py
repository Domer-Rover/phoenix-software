# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
Wrap-safe 16-bit sequence numbers.

Sequence numbers count 0..65535 and then wrap to 0. Comparisons use
modulo-65536 arithmetic (RFC 1982 serial number arithmetic), so 0 counts as
newer than 65535 and the counter never runs out.
"""

SEQ_BITS = 16
SEQ_MOD = 1 << SEQ_BITS
HALF = SEQ_MOD // 2


def next_seq(seq):
    """Return the sequence number after ``seq``, wrapping to 0."""
    return (seq + 1) % SEQ_MOD


def distance(a, b):
    """
    Return the signed number of steps from ``a`` to ``b``.

    The result is in -32768..32767: positive when ``b`` comes after ``a``.
    """
    d = (b - a) % SEQ_MOD
    return d - SEQ_MOD if d >= HALF else d


def is_newer(a, b):
    """Return True if ``a`` comes after ``b``."""
    return distance(b, a) > 0
