# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""Tests for Link: delivery, acknowledgements, resends, sessions and liveness."""

import unittest

from roverlink.protocol.link import (
    Delivered, Failed, Link, LinkDown, LinkUp, PeerRestarted)

DRIVE = 2       # stream purpose
WAYPOINT = 13   # reliable purpose


class Wire:
    """Two Links joined in memory, with a fake clock and switchable loss."""

    def __init__(self, a=None, b=None):
        self.a = a or Link(session=1)
        self.b = b or Link(session=2)
        self.now = 0.0
        self.drop_a_to_b = False
        self.drop_b_to_a = False
        self.events = {'a': [], 'b': []}

    def step(self, dt=0.0):
        """Advance the clock, then exchange bytes until both ends are quiet."""
        self.now += dt
        while True:
            out_a, ev_a = self.a.poll(self.now)
            out_b, ev_b = self.b.poll(self.now)
            self.events['a'] += ev_a
            self.events['b'] += ev_b
            if not out_a and not out_b:
                return
            if not self.drop_a_to_b:
                self.b.feed(out_a, self.now)
            if not self.drop_b_to_a:
                self.a.feed(out_b, self.now)

    def run(self, seconds, dt=0.1):
        for _ in range(round(seconds / dt)):
            self.step(dt)

    def take(self, side, kind):
        found = [e for e in self.events[side] if isinstance(e, kind)]
        self.events[side] = [e for e in self.events[side] if not isinstance(e, kind)]
        return found


class TestDelivery(unittest.TestCase):

    def test_reliable_delivered_once_and_acked(self):
        wire = Wire()
        wire.a.send(WAYPOINT, b'goal', reliable=True, now=wire.now)
        wire.run(3.0)
        self.assertEqual(wire.take('b', Delivered), [Delivered(WAYPOINT, b'goal')])
        self.assertEqual(wire.a.stats()['acked'], 1)
        self.assertEqual(wire.a.stats()['resent'], 0)
        self.assertEqual(wire.a.stats()['pending'], 0)
        self.assertEqual(wire.take('a', Failed), [])

    def test_resent_until_acked_then_never_again(self):
        wire = Wire()
        wire.drop_a_to_b = True
        wire.a.send(WAYPOINT, b'goal', reliable=True, now=wire.now)
        wire.run(1.2)  # resends at 0.5 and 1.0, both lost
        self.assertEqual(wire.a.stats()['resent'], 2)
        wire.drop_a_to_b = False
        wire.run(0.5)
        self.assertEqual(len(wire.take('b', Delivered)), 1)
        resent = wire.a.stats()['resent']
        wire.run(5.0)
        self.assertEqual(wire.a.stats()['resent'], resent)

    def test_failed_after_max_retries(self):
        wire = Wire(a=Link(session=1, rto=0.5, max_retries=3, ttl=60.0))
        wire.drop_a_to_b = True
        seq = wire.a.send(WAYPOINT, b'goal', reliable=True, now=wire.now)
        wire.run(1.9)
        self.assertEqual(wire.take('a', Failed), [])
        wire.run(0.2)  # fourth deadline at 2.0 s: 3 resends used up
        self.assertEqual(wire.take('a', Failed), [Failed(seq, WAYPOINT)])
        self.assertEqual(wire.a.stats()['resent'], 3)

    def test_failed_after_ttl(self):
        wire = Wire(a=Link(session=1, rto=1.0, max_retries=100, ttl=2.5))
        wire.drop_a_to_b = True
        seq = wire.a.send(WAYPOINT, b'goal', reliable=True, now=wire.now)
        wire.run(2.9)
        self.assertEqual(wire.take('a', Failed), [])
        wire.run(0.2)  # first deadline after the 2.5 s ttl is 3.0 s
        self.assertEqual(wire.take('a', Failed), [Failed(seq, WAYPOINT)])

    def test_lost_ack_resent_but_not_delivered_twice(self):
        wire = Wire()
        wire.drop_b_to_a = True
        wire.a.send(WAYPOINT, b'goal', reliable=True, now=wire.now)
        wire.run(1.2)
        wire.drop_b_to_a = False
        wire.run(1.0)
        self.assertEqual(len(wire.take('b', Delivered)), 1)
        self.assertGreater(wire.b.stats()['duplicates'], 0)
        self.assertEqual(wire.a.stats()['pending'], 0)

    def test_stream_never_resent(self):
        wire = Wire()
        wire.drop_a_to_b = True
        wire.a.send(DRIVE, b'\x00\x64\x00\x3c', now=wire.now)
        wire.run(5.0)
        self.assertEqual(wire.a.stats()['resent'], 0)
        self.assertEqual(wire.a.stats()['pending'], 0)

    def test_stream_delivered(self):
        wire = Wire()
        for i in range(3):
            wire.a.send(DRIVE, bytes([i]), now=wire.now)
        wire.step()
        self.assertEqual([d.payload for d in wire.take('b', Delivered)],
                         [b'\x00', b'\x01', b'\x02'])

    def test_heavy_stream_traffic_does_not_age_out_reliable(self):
        wire = Wire()
        wire.drop_a_to_b = True
        wire.a.send(WAYPOINT, b'goal', reliable=True, now=wire.now)
        wire.step()
        wire.drop_a_to_b = False
        for _ in range(3000):  # far more than the 1024-wide window
            wire.a.send(DRIVE, b'', now=wire.now)
        wire.run(1.0)
        delivered = [d for d in wire.take('b', Delivered) if d.purpose == WAYPOINT]
        self.assertEqual(delivered, [Delivered(WAYPOINT, b'goal')])

    def test_reserved_purpose_rejected(self):
        with self.assertRaises(ValueError):
            Link().send(0, b'', now=0.0)
        with self.assertRaises(ValueError):
            Link().send(3, b'', now=0.0)

    def test_sequence_wraps_without_failing(self):
        link = Link(session=1)
        for _ in range(70_000):
            link.send(DRIVE, b'', now=0.0)
        self.assertEqual(link.stats()['sent'], 70_000)


class TestSessions(unittest.TestCase):

    def test_peer_restart_detected_and_messages_flow(self):
        wire = Wire()
        wire.a.send(WAYPOINT, b'first', reliable=True, now=wire.now)
        wire.run(1.0)
        wire.take('b', Delivered)

        # Restart A: new session, sequence numbers start from 0 again
        wire.a = Link(session=99)
        wire.a.send(WAYPOINT, b'after restart', reliable=True, now=wire.now)
        wire.run(1.0)

        self.assertEqual(wire.take('b', PeerRestarted), [PeerRestarted(1, 99)])
        self.assertEqual(wire.take('b', Delivered), [Delivered(WAYPOINT, b'after restart')])


class TestLiveness(unittest.TestCase):

    def test_heartbeat_every_period(self):
        wire = Wire()
        wire.run(3.5, dt=0.05)
        # Heartbeats at about t=0.05, 1.05, 2.05, 3.05 keep B's link up
        self.assertEqual(wire.b.stats()['received'], 4)
        self.assertTrue(wire.b.link_up)

    def test_heartbeats_not_delivered(self):
        wire = Wire()
        wire.run(3.0)
        self.assertEqual(wire.take('b', Delivered), [])

    def test_link_down_then_up(self):
        wire = Wire()
        wire.step(0.1)
        self.assertEqual(len(wire.take('b', LinkUp)), 1)

        wire.drop_a_to_b = True
        wire.run(2.8)
        self.assertEqual(wire.take('b', LinkDown), [])
        wire.run(0.3)
        self.assertEqual(len(wire.take('b', LinkDown)), 1)
        self.assertFalse(wire.b.link_up)

        wire.drop_a_to_b = False
        wire.run(1.1)
        self.assertEqual(len(wire.take('b', LinkUp)), 1)
        self.assertTrue(wire.b.link_up)

    def test_next_wakeup_is_earliest_deadline(self):
        link = Link(session=1, rto=0.5, heartbeat_period=1.0)
        self.assertEqual(link.next_wakeup(), float('-inf'))
        link.poll(10.0)  # first heartbeat; next one due at 11.0
        self.assertEqual(link.next_wakeup(), 11.0)
        link.send(WAYPOINT, b'', reliable=True, now=10.2)
        self.assertEqual(link.next_wakeup(), 10.7)


if __name__ == '__main__':
    unittest.main()
