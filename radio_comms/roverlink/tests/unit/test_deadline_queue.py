# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""Tests for the deadline min-heap."""

import heapq
import random
import unittest

from roverlink.protocol.deadline_queue import DeadlineQueue


def assert_heap_order(test, queue):
    heap = queue._heap
    for i in range(1, len(heap)):
        test.assertLessEqual(heap[(i - 1) // 2][:2], heap[i][:2])


class TestDeadlineQueue(unittest.TestCase):

    def test_empty(self):
        queue = DeadlineQueue()
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.peek_deadline())
        self.assertEqual(queue.pop_due(1e9), [])
        with self.assertRaises(IndexError):
            queue.pop()

    def test_single(self):
        queue = DeadlineQueue()
        queue.push(2.0, 'a')
        self.assertEqual(queue.peek_deadline(), 2.0)
        self.assertEqual(queue.pop_due(1.9), [])
        self.assertEqual(queue.pop_due(2.0), ['a'])
        self.assertEqual(len(queue), 0)

    def test_equal_deadlines_fifo(self):
        queue = DeadlineQueue()
        for name in 'abcdef':
            queue.push(1.0, name)
        self.assertEqual(queue.pop_due(1.0), list('abcdef'))

    def test_pop_due_order(self):
        queue = DeadlineQueue()
        for deadline in (5, 1, 4, 2, 3):
            queue.push(deadline, deadline)
        self.assertEqual(queue.pop_due(3), [1, 2, 3])
        self.assertEqual(queue.peek_deadline(), 4)

    def test_heap_order_after_every_operation(self):
        rng = random.Random(3)
        queue = DeadlineQueue()
        for _ in range(500):
            if len(queue) and rng.random() < 0.4:
                queue.pop()
            else:
                queue.push(rng.randrange(20), None)
            assert_heap_order(self, queue)

    def test_matches_heapq(self):
        rng = random.Random(4)
        queue = DeadlineQueue()
        oracle = []
        counter = 0
        for _ in range(10_000):
            if oracle and rng.random() < 0.45:
                expected = heapq.heappop(oracle)
                self.assertEqual(queue.pop(), (expected[0], expected[2]))
            else:
                deadline = rng.randrange(1000) / 10
                heapq.heappush(oracle, (deadline, counter, counter))
                queue.push(deadline, counter)
                counter += 1
            self.assertEqual(len(queue), len(oracle))
            self.assertEqual(queue.peek_deadline(), oracle[0][0] if oracle else None)


if __name__ == '__main__':
    unittest.main()
