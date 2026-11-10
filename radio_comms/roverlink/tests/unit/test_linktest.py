# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""Tests for the UDP link test tool."""

import os
import random
import socket
import subprocess
import sys
import tempfile
import unittest

from roverlink.protocol.frame import encode, Frame, StreamDecoder
from roverlink.protocol.link import Link
from roverlink.tools.linktest import Impairment, LinkTester, split_frames

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Pair:
    """Two testers joined in memory with a fake clock and simulated loss."""

    def __init__(self, loss=0.0, seed=1):
        self.now = 0.0
        self.lines = {'a': [], 'b': []}
        self.a = self._tester('a', 1, loss, seed)
        self.b = self._tester('b', 2, loss, seed + 1)

    def _tester(self, name, session, loss, seed):
        return LinkTester(Link(session=session), self.lines[name].append,
                          impairment=Impairment(loss, 0.0, random.Random(seed)))

    def run(self, seconds, dt=0.01):
        for _ in range(round(seconds / dt)):
            self.now += dt
            out_a = self.a.tick(self.now)
            out_b = self.b.tick(self.now)
            self._deliver(self.a, self.b, out_a)
            self._deliver(self.b, self.a, out_b)

    def _deliver(self, sender, receiver, data):
        for datagram in split_frames(data):
            datagram = sender.impairment.apply(datagram)
            if datagram is not None:
                receiver.receive(datagram, self.now)

    def output(self, name):
        return '\n'.join(self.lines[name])


class TestSplitFrames(unittest.TestCase):

    def test_whole_frames_and_size_limit(self):
        frames = [Frame(1, i, 200, 0, 0, bytes(100)) for i in range(40)]
        data = b''.join(encode(f) for f in frames)
        datagrams = split_frames(data, max_size=500)
        self.assertGreater(len(datagrams), 1)
        self.assertEqual(b''.join(datagrams), data)
        decoded = []
        for datagram in datagrams:
            self.assertLessEqual(len(datagram), 500)
            # Each datagram decodes on its own: no frame is split across two.
            decoded += StreamDecoder().feed(datagram)
        self.assertEqual(decoded, frames)

    def test_oversized_frame_gets_its_own_datagram(self):
        big = encode(Frame(1, 0, 200, 0, 0, bytes(3000)))
        small = encode(Frame(1, 1, 200, 0, 0, b'x'))
        self.assertEqual(split_frames(small + big + small, max_size=1200),
                         [small, big, small])

    def test_empty(self):
        self.assertEqual(split_frames(b''), [])


class TestImpairment(unittest.TestCase):

    def test_loss_rate(self):
        impairment = Impairment(loss=0.3, rng=random.Random(5))
        kept = sum(impairment.apply(b'x' * 10) is not None for _ in range(10_000))
        self.assertAlmostEqual(kept / 10_000, 0.7, delta=0.02)
        self.assertEqual(impairment.dropped, 10_000 - kept)

    def test_corruption_changes_one_byte(self):
        impairment = Impairment(corrupt=1.0, rng=random.Random(6))
        original = bytes(range(50))
        damaged = impairment.apply(original)
        self.assertEqual(sum(a != b for a, b in zip(original, damaged)), 1)


class TestCommands(unittest.TestCase):

    def test_send_and_stream_delivered(self):
        pair = Pair()
        pair.a.command('send hello rover', pair.now)
        pair.a.command('stream drive 10 10', pair.now)
        pair.run(1.0)
        self.assertIn("<- text 'hello rover'", pair.output('b'))
        self.assertIn("<- stream text 'drive 10 10'", pair.output('b'))
        self.assertIn('LINK UP', pair.output('a'))

    def test_ping_summary(self):
        pair = Pair()
        pair.a.command('ping 3', pair.now)
        pair.run(2.0)
        self.assertIn('ping: 3 sent, 3 replies, 0 lost', pair.output('a'))
        self.assertIsNone(pair.a.ping)

    def test_burst_under_loss_never_silently_lost(self):
        pair = Pair(loss=0.3)
        pair.a.command('burst 200', pair.now)
        pair.run(25.0)
        tx = pair.a.bursts_sent[1]
        (rx,) = pair.b.bursts_received.values()
        self.assertTrue(tx.done and rx.done)
        self.assertEqual(tx.acked + tx.failed, 200)
        self.assertEqual(rx.delivered_twice, 0)
        # Anything the receiver never got must have been reported as failed.
        self.assertLessEqual(200 - len(rx.seen), tx.failed)
        self.assertGreater(pair.a.link.stats()['resent'], 0)
        self.assertIn('burst 1 sent: 200 messages', pair.output('a'))
        self.assertIn('burst 1 received:', pair.output('b'))

    def test_clean_burst_is_complete(self):
        pair = Pair()
        pair.a.command('burst 1500 64', pair.now)
        pair.run(5.0)
        self.assertIn('burst 1 sent: 1500 messages, 1500 acked, 0 failed', pair.output('a'))
        self.assertIn('burst 1 received: 1500/1500, 0 missing, 0 delivered twice',
                      pair.output('b'))

    def test_spam_counts_loss_and_is_never_resent(self):
        pair = Pair(loss=0.3)
        pair.a.command('spam 50', pair.now)
        pair.run(6.0)
        pair.a.command('stop', pair.now)
        pair.run(2.0)
        rx = pair.b.spam_rx
        self.assertGreater(rx.received, 0)
        self.assertGreater(rx.lost, 0)
        self.assertEqual(pair.a.link.stats()['resent'], 0)
        self.assertIn('spam:', pair.output('b'))

    def test_peer_restart_reported(self):
        pair = Pair()
        pair.run(0.5)
        pair.a = LinkTester(Link(session=99), pair.lines['a'].append)
        pair.run(0.5)
        self.assertIn('PEER RESTARTED (session 0x0001 -> 0x0063)', pair.output('b'))

    def test_bad_commands_print_usage(self):
        pair = Pair()
        for line in ('send', 'burst', 'burst x', 'burst 10 3', 'ping 0', 'spam 0',
                     'loss 2', 'frobnicate', ''):
            pair.a.command(line, pair.now)
        out = pair.output('a')
        self.assertEqual(out.count('usage:'), 7)
        self.assertIn("unknown command 'frobnicate'", out)

    def test_loss_command_changes_impairment(self):
        pair = Pair()
        pair.a.command('loss 0.25', pair.now)
        self.assertEqual(pair.a.impairment.loss, 0.25)

    def test_quit(self):
        pair = Pair()
        pair.a.command('quit', pair.now)
        self.assertTrue(pair.a.done)


class TestUdpEndToEnd(unittest.TestCase):
    """Run both ends as real processes over localhost UDP."""

    def test_two_processes(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(('127.0.0.1', 0))
            port = str(probe.getsockname()[1])
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, 'commands.txt')
            with open(script, 'w', encoding='utf-8') as f:
                f.write('sleep 0.5\nsend hello over udp\nping 2\nburst 50\n')
            base = [sys.executable, '-m', 'roverlink.tools.linktest']
            options = dict(cwd=PACKAGE_ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            listener = subprocess.Popen(
                base + ['listen', '--bind', '127.0.0.1', '--port', port, '--duration', '4'],
                **options)
            connector = subprocess.Popen(
                base + ['connect', '127.0.0.1', '--port', port, '--duration', '3.5',
                        '--script', script], **options)
            out_c, _ = connector.communicate(timeout=20)
            out_l, _ = listener.communicate(timeout=20)
        self.assertEqual(connector.returncode, 0, out_c)
        self.assertEqual(listener.returncode, 0, out_l)
        self.assertIn('LINK UP', out_c)
        self.assertIn('LINK UP', out_l)
        self.assertIn("<- text 'hello over udp'", out_l)
        self.assertIn('ping: 2 sent, 2 replies, 0 lost', out_c)
        self.assertIn('burst 1 received: 50/50', out_l)


if __name__ == '__main__':
    unittest.main()
