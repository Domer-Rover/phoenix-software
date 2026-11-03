# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
Min-heap of items keyed by deadline.

The link keeps every unacknowledged reliable frame here, keyed by when it is
next due for a resend. The most urgent deadline is always at index 0, so the
sender can sleep until exactly that time instead of polling.

Items with equal deadlines come out in the order they were pushed. Removal is
lazy: callers mark an item as done and skip it when it is popped, which keeps
every operation O(log n).
"""


class DeadlineQueue:
    """Array-backed binary min-heap of ``(deadline, item)``."""

    def __init__(self):
        self._heap = []
        self._counter = 0

    def __len__(self):
        return len(self._heap)

    def push(self, deadline, item):
        """Add ``item`` due at ``deadline``. O(log n)."""
        self._heap.append((deadline, self._counter, item))
        self._counter += 1
        self._sift_up(len(self._heap) - 1)

    def peek_deadline(self):
        """Return the earliest deadline, or None if empty. O(1)."""
        return self._heap[0][0] if self._heap else None

    def pop(self):
        """Remove and return ``(deadline, item)`` with the earliest deadline."""
        if not self._heap:
            raise IndexError('pop from empty DeadlineQueue')
        last = self._heap.pop()
        if not self._heap:
            return last[0], last[2]
        top = self._heap[0]
        self._heap[0] = last
        self._sift_down(0)
        return top[0], top[2]

    def pop_due(self, now):
        """Remove and return every item with ``deadline <= now``, earliest first."""
        due = []
        while self._heap and self._heap[0][0] <= now:
            due.append(self.pop()[1])
        return due

    def _sift_up(self, i):
        heap = self._heap
        entry = heap[i]
        while i > 0:
            parent = (i - 1) // 2
            if heap[parent][:2] <= entry[:2]:
                break
            heap[i] = heap[parent]
            i = parent
        heap[i] = entry

    def _sift_down(self, i):
        heap = self._heap
        n = len(heap)
        entry = heap[i]
        while True:
            child = 2 * i + 1
            if child >= n:
                break
            right = child + 1
            if right < n and heap[right][:2] < heap[child][:2]:
                child = right
            if entry[:2] <= heap[child][:2]:
                break
            heap[i] = heap[child]
            i = child
        heap[i] = entry
