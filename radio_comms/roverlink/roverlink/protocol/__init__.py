# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
Transport-independent message protocol.

Nothing in this package reads the clock, starts threads or does I/O. Callers
pass in received bytes and the current time, and write out the bytes it
returns. That keeps every behaviour testable with a fake clock.
"""
