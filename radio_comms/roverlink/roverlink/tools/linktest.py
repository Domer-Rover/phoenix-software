# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
Test the roverlink protocol between two machines over UDP.

Start one end with ``listen`` (for example on the rover) and the other with
``connect HOST`` (for example on the base station laptop), then type commands
such as ``send hello``, ``ping 10`` or ``burst 500``. The full procedure is in
``docs/roverlink-link-test.md`` at the repository root.

UDP is used on purpose. TCP retransmits lost data by itself, which would hide
the protocol's own acknowledgements and resends. UDP drops datagrams the way a
radio drops packets, so whatever arrives intact is down to the protocol. On a
clean network, ``--loss`` and ``--corrupt`` add simulated damage.

Only ``run`` reads the clock, sockets and stdin. ``LinkTester`` takes the time
as an argument, like ``Link`` does, so the unit tests drive it with a fake
clock.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import queue
import random
import socket
import struct
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..protocol.frame import CRC_SIZE, HEADER_SIZE, MAX_PAYLOAD
from ..protocol.link import Delivered, Failed, Link, LinkDown, LinkUp, PeerRestarted

# Test-only purposes. They sit in the 128-255 range that the comms plan keeps
# free of real routes, so they never clash with actual commands or telemetry.
PURPOSE_TEXT = 200          # reliable text
PURPOSE_STREAM_TEXT = 201   # stream text
PURPOSE_PING = 202
PURPOSE_PONG = 203
PURPOSE_BURST = 204
PURPOSE_SPAM = 205

_PING = struct.Struct('>I')     # ping id (echoed back in the pong)
_BURST = struct.Struct('>HII')  # burst id, message index, messages in burst
_SPAM = struct.Struct('>I')     # counter

DEFAULT_PORT = 5005
MAX_DATAGRAM = 1200   # stays under a typical 1500-byte Ethernet MTU
PING_INTERVAL = 0.2
BURST_PER_TICK = 50
# The receiver's duplicate window covers the last 1024 reliable sequence
# numbers. Keeping at most half that many messages unacknowledged means a
# resend never arrives so late that the receiver has to reject it as too old.
MAX_IN_FLIGHT = 512
SPAM_REPORT_PERIOD = 5.0

HELP = """\
commands:
  send TEXT        reliable text message (acknowledged, resent if lost)
  stream TEXT      stream text message (sent once, never resent)
  ping [N]         N reliable pings (default 1), prints round-trip times
  burst N [SIZE]   N reliable messages of SIZE bytes (default 32); both ends
                   print a summary when it is over
  spam HZ [SIZE]   stream messages at HZ per second until 'stop'; the other
                   end reports how many were lost every 5 s
  stop             stop 'spam'
  loss P           drop outgoing datagrams with probability P (0 to 1)
  corrupt P        damage one byte of outgoing datagrams with probability P
  status           link state, sessions and unacknowledged messages
  stats            protocol counters
  help             this text
  quit             exit (Ctrl+C also works)
in --script files only:
  sleep S          wait S seconds before the next line"""


class _Usage(Exception):
    """A command was typed with the wrong arguments."""


def _parse(rest: str, usage: str, types: Tuple[type, ...], required: int) -> list:
    """Split ``rest`` into arguments of ``types``, or raise ``_Usage``."""
    words = rest.split()
    if not required <= len(words) <= len(types):
        raise _Usage(usage)
    try:
        return [kind(word) for kind, word in zip(types, words)]
    except ValueError:
        raise _Usage(usage) from None


def _probability(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f'{text} is not between 0 and 1')
    return value


def split_frames(data: bytes, max_size: int = MAX_DATAGRAM) -> List[bytes]:
    """
    Group whole frames from ``Link.poll`` output into datagrams.

    Each datagram holds at most ``max_size`` bytes, except that a single frame
    larger than that gets a datagram of its own. Keeping frames whole means a
    lost datagram loses whole frames, never half of one.
    """
    datagrams = []
    start = i = 0
    while i < len(data):
        length = int.from_bytes(data[i + HEADER_SIZE - 2:i + HEADER_SIZE], 'big')
        end = i + HEADER_SIZE + length + CRC_SIZE
        if end - start > max_size and i > start:
            datagrams.append(data[start:i])
            start = i
        i = end
    if start < len(data):
        datagrams.append(data[start:])
    return datagrams


class Impairment:
    """Simulated loss and corruption applied to outgoing datagrams."""

    def __init__(self, loss: float = 0.0, corrupt: float = 0.0,
                 rng: Optional[random.Random] = None) -> None:
        self.loss = loss
        self.corrupt = corrupt
        self.rng = rng or random.Random()
        self.dropped = 0
        self.corrupted = 0

    def apply(self, datagram: bytes) -> Optional[bytes]:
        """Return the datagram, a damaged copy of it, or None to drop it."""
        if self.loss and self.rng.random() < self.loss:
            self.dropped += 1
            return None
        if self.corrupt and self.rng.random() < self.corrupt:
            damaged = bytearray(datagram)
            damaged[self.rng.randrange(len(damaged))] ^= self.rng.randrange(1, 256)
            self.corrupted += 1
            return bytes(damaged)
        return datagram


@dataclass
class PingRun:
    """State of one ``ping N`` command."""

    total: int
    remaining: int
    next_send: float
    last_send: float = 0.0
    sent_at: Dict[int, float] = field(default_factory=dict)  # ping id -> time
    rtts: List[float] = field(default_factory=list)


@dataclass
class BurstTx:
    """Sender side of one ``burst`` command."""

    burst_id: int
    total: int
    size: int
    started: float
    resent_before: int
    sent: int = 0
    failed: int = 0
    last_send: float = 0.0
    done: bool = False

    @property
    def acked(self) -> int:
        """Messages acknowledged; final once ``done`` is True."""
        return self.sent - self.failed


@dataclass
class BurstRx:
    """Receiver side of one burst."""

    total: int
    first: float
    last: float
    seen: Set[int] = field(default_factory=set)
    delivered_twice: int = 0
    done: bool = False


@dataclass
class SpamRx:
    """Receiver side of ``spam``: counts gaps in the stream counter."""

    expected: Optional[int] = None
    received: int = 0
    lost: int = 0
    window_received: int = 0
    window_lost: int = 0
    window_start: float = 0.0
    last: float = 0.0


class LinkTester:
    """
    The test commands and their bookkeeping, on top of one ``Link``.

    The caller feeds received datagrams to ``receive``, sends whatever ``tick``
    returns, and passes typed lines to ``command``. Output goes to ``out``.
    """

    def __init__(self, link: Link, out: Callable[[str], None], *,
                 impairment: Optional[Impairment] = None, quiet: bool = False) -> None:
        self.link = link
        self.impairment = impairment or Impairment()
        self.quiet = quiet
        self.done = False
        self._out = out
        self._start: Optional[float] = None

        # Reliable seq -> (kind, reference, send time), to explain Failed events.
        self._sent: Dict[int, Tuple[str, int, float]] = {}
        self._next_prune = 0.0

        self.ping: Optional[PingRun] = None
        self._next_ping_id = 1

        self.bursts_sent: Dict[int, BurstTx] = {}
        self._burst: Optional[BurstTx] = None   # the burst still being sent
        self._next_burst_id = 1
        # (peer session, burst id) -> state, so a peer restart starts afresh.
        self.bursts_received: Dict[Tuple[Optional[int], int], BurstRx] = {}

        self._spam: Optional[Tuple[float, int]] = None   # (hz, size)
        self._spam_next = 0.0
        self._spam_counter = 0
        self.spam_rx = SpamRx()

        self._commands: Dict[str, Callable[[str, float], None]] = {
            'send': self._cmd_send, 'stream': self._cmd_stream,
            'ping': self._cmd_ping, 'burst': self._cmd_burst,
            'spam': self._cmd_spam, 'stop': self._cmd_stop,
            'loss': self._cmd_loss, 'corrupt': self._cmd_corrupt,
            'status': self._cmd_status, 'stats': self._cmd_stats,
            'help': self._cmd_help, 'quit': self._cmd_quit, 'exit': self._cmd_quit,
        }

    # ---- input -------------------------------------------------------------

    def say(self, now: float, message: str) -> None:
        """Print ``message`` with the time since the tester started."""
        self._mark(now)
        self._out(f'[{now - self._start:8.2f}] {message}')

    def command(self, line: str, now: float) -> None:
        """Run one typed command. Mistakes print a usage line, never raise."""
        self._mark(now)
        words = line.strip().split(maxsplit=1)
        if not words:
            return
        name = words[0].lower()
        rest = words[1] if len(words) > 1 else ''
        handler = self._commands.get(name)
        if handler is None:
            self.say(now, f"unknown command {name!r}; type 'help'")
            return
        try:
            handler(rest, now)
        except _Usage as exc:
            self.say(now, f'usage: {exc}')

    def receive(self, data: bytes, now: float) -> None:
        """Pass a received datagram to the link."""
        self._mark(now)
        self.link.feed(data, now)

    def tick(self, now: float) -> bytes:
        """Run due work and return the bytes to send to the peer."""
        self._mark(now)
        self._run_ping(now)
        self._run_burst(now)
        self._run_spam(now)
        out = bytearray()
        while True:
            # Handling an event can send (a pong, for example), so poll until
            # there are no events left and everything queued goes out now.
            data, events = self.link.poll(now)
            out += data
            if not events:
                break
            for event in events:
                self._on_event(event, now)
        self._check_summaries(now)
        self._prune(now)
        return bytes(out)

    # ---- commands ----------------------------------------------------------

    def _cmd_send(self, rest: str, now: float) -> None:
        payload = rest.encode()
        if not payload or len(payload) > MAX_PAYLOAD:
            raise _Usage(f'send TEXT  (1 to {MAX_PAYLOAD} bytes)')
        seq = self.link.send(PURPOSE_TEXT, payload, reliable=True, now=now)
        self._track(seq, 'text', 0, now)
        self.say(now, f'-> text {rest!r} (seq {seq})')

    def _cmd_stream(self, rest: str, now: float) -> None:
        payload = rest.encode()
        if not payload or len(payload) > MAX_PAYLOAD:
            raise _Usage(f'stream TEXT  (1 to {MAX_PAYLOAD} bytes)')
        seq = self.link.send(PURPOSE_STREAM_TEXT, payload, now=now)
        self.say(now, f'-> stream text {rest!r} (seq {seq})')

    def _cmd_ping(self, rest: str, now: float) -> None:
        args = _parse(rest, 'ping [N]', (int,), required=0)
        count = args[0] if args else 1
        if count < 1:
            raise _Usage('ping [N]  (N at least 1)')
        if self.ping is not None:
            self.say(now, 'a ping is still running; wait for its summary')
            return
        self.ping = PingRun(total=count, remaining=count, next_send=now)

    def _cmd_burst(self, rest: str, now: float) -> None:
        usage = f'burst N [SIZE]  (SIZE {_BURST.size} to {MAX_PAYLOAD} bytes)'
        args = _parse(rest, usage, (int, int), required=1)
        count, size = args[0], (args[1] if len(args) > 1 else 32)
        if count < 1 or not _BURST.size <= size <= MAX_PAYLOAD:
            raise _Usage(usage)
        if self._burst is not None:
            self.say(now, 'a burst is still being sent; wait for it to finish')
            return
        burst_id = self._next_burst_id
        self._next_burst_id = burst_id % 0xFFFF + 1
        tx = BurstTx(burst_id, count, size, started=now,
                     resent_before=self.link.stats()['resent'])
        self.bursts_sent[burst_id] = tx
        self._burst = tx
        self.say(now, f'burst {burst_id}: sending {count} reliable messages of {size} bytes')

    def _cmd_spam(self, rest: str, now: float) -> None:
        usage = f'spam HZ [SIZE]  (HZ 0 to 1000, SIZE {_SPAM.size} to {MAX_PAYLOAD})'
        args = _parse(rest, usage, (float, int), required=1)
        hz, size = args[0], (args[1] if len(args) > 1 else 32)
        if not 0 < hz <= 1000 or not _SPAM.size <= size <= MAX_PAYLOAD:
            raise _Usage(usage)
        self._spam = (hz, size)
        self._spam_next = now
        self._spam_counter = 0
        self.say(now, f'spam: sending {hz:g} stream messages per second; type stop to end')

    def _cmd_stop(self, rest: str, now: float) -> None:
        if self._spam is None:
            self.say(now, 'spam is not running')
            return
        self._spam = None
        self.say(now, f'spam stopped after {self._spam_counter} messages')

    def _cmd_loss(self, rest: str, now: float) -> None:
        (self.impairment.loss,) = _parse(rest, 'loss P  (0 to 1)', (_probability,), required=1)
        self.say(now, f'simulated loss is now {self.impairment.loss:g}')

    def _cmd_corrupt(self, rest: str, now: float) -> None:
        (self.impairment.corrupt,) = _parse(rest, 'corrupt P  (0 to 1)', (_probability,),
                                            required=1)
        self.say(now, f'simulated corruption is now {self.impairment.corrupt:g}')

    def _cmd_status(self, rest: str, now: float) -> None:
        link = self.link
        peer = 'none yet' if link.peer_session is None else f'0x{link.peer_session:04x}'
        self.say(now, f"link {'UP' if link.link_up else 'DOWN'}, session 0x{link.session:04x}, "
                      f"peer {peer}, {link.stats()['pending']} unacknowledged, "
                      f'loss {self.impairment.loss:g}, corrupt {self.impairment.corrupt:g}')

    def _cmd_stats(self, rest: str, now: float) -> None:
        stats = self.link.stats()
        stats['sim_dropped'] = self.impairment.dropped
        stats['sim_corrupted'] = self.impairment.corrupted
        self.say(now, 'stats ' + ' '.join(f'{k}={v}' for k, v in stats.items()))

    def _cmd_help(self, rest: str, now: float) -> None:
        for line in HELP.splitlines():
            self._out(line)

    def _cmd_quit(self, rest: str, now: float) -> None:
        self.done = True

    # ---- sending -----------------------------------------------------------

    def _run_ping(self, now: float) -> None:
        run = self.ping
        if run is None or run.remaining == 0 or now < run.next_send:
            return
        ping_id = self._next_ping_id
        self._next_ping_id = (ping_id + 1) % (1 << 32)
        seq = self.link.send(PURPOSE_PING, _PING.pack(ping_id), reliable=True, now=now)
        self._track(seq, 'ping', ping_id, now)
        run.sent_at[ping_id] = now
        run.remaining -= 1
        run.last_send = now
        run.next_send = now + PING_INTERVAL

    def _run_burst(self, now: float) -> None:
        tx = self._burst
        if tx is None or tx.sent == tx.total:
            return
        room = MAX_IN_FLIGHT - self.link.stats()['pending']
        count = min(BURST_PER_TICK, tx.total - tx.sent, room)
        padding = bytes(tx.size - _BURST.size)
        for _ in range(count):
            payload = _BURST.pack(tx.burst_id, tx.sent, tx.total) + padding
            seq = self.link.send(PURPOSE_BURST, payload, reliable=True, now=now)
            self._track(seq, 'burst', tx.burst_id, now)
            tx.sent += 1
        if count > 0:
            tx.last_send = now

    def _run_spam(self, now: float) -> None:
        if self._spam is None:
            return
        hz, size = self._spam
        padding = bytes(size - _SPAM.size)
        sent = 0
        while now >= self._spam_next and sent < 100:
            payload = _SPAM.pack(self._spam_counter % (1 << 32)) + padding
            self.link.send(PURPOSE_SPAM, payload, now=now)
            self._spam_counter += 1
            self._spam_next += 1.0 / hz
            sent += 1
        if now - self._spam_next > 1.0:
            # Fell far behind (the process was paused): don't flood to catch up.
            self._spam_next = now

    def _track(self, seq: int, kind: str, ref: int, now: float) -> None:
        self._sent[seq] = (kind, ref, now)

    # ---- receiving ---------------------------------------------------------

    def _on_event(self, event: object, now: float) -> None:
        link = self.link
        if isinstance(event, Delivered):
            self._on_delivered(event, now)
        elif isinstance(event, Failed):
            kind, ref, _ = self._sent.pop(event.seq, ('message', 0, now))
            if kind == 'burst' and ref in self.bursts_sent:
                self.bursts_sent[ref].failed += 1
            if not (self.quiet and kind == 'burst'):
                self.say(now, f'FAILED seq {event.seq}: {kind} was never acknowledged')
        elif isinstance(event, LinkUp):
            self.say(now, f'LINK UP (peer session 0x{link.peer_session:04x})')
        elif isinstance(event, LinkDown):
            self.say(now, f'LINK DOWN (nothing received for {link.link_timeout:g} s)')
        elif isinstance(event, PeerRestarted):
            self.say(now, f'PEER RESTARTED (session 0x{event.old_session:04x} '
                          f'-> 0x{event.new_session:04x})')
            self.spam_rx.expected = None

    def _on_delivered(self, event: Delivered, now: float) -> None:
        purpose, payload = event.purpose, event.payload
        try:
            if purpose == PURPOSE_TEXT:
                self.say(now, f"<- text {payload.decode('utf-8', 'replace')!r}")
            elif purpose == PURPOSE_STREAM_TEXT:
                self.say(now, f"<- stream text {payload.decode('utf-8', 'replace')!r}")
            elif purpose == PURPOSE_PING:
                seq = self.link.send(PURPOSE_PONG, payload, reliable=True, now=now)
                self._track(seq, 'pong', 0, now)
            elif purpose == PURPOSE_PONG:
                self._on_pong(payload, now)
            elif purpose == PURPOSE_BURST:
                self._on_burst(payload, now)
            elif purpose == PURPOSE_SPAM:
                self._on_spam(payload, now)
            else:
                self.say(now, f'<- unknown purpose {purpose} ({len(payload)} bytes)')
        except struct.error:
            self.say(now, f'<- malformed purpose {purpose} message ({len(payload)} bytes)')

    def _on_pong(self, payload: bytes, now: float) -> None:
        (ping_id,) = _PING.unpack_from(payload)
        run = self.ping
        if run is None or ping_id not in run.sent_at:
            return   # a late reply to a ping that was already given up on
        rtt = now - run.sent_at.pop(ping_id)
        run.rtts.append(rtt)
        if not self.quiet:
            self.say(now, f'pong {ping_id}: {rtt * 1000:.1f} ms')

    def _on_burst(self, payload: bytes, now: float) -> None:
        burst_id, index, total = _BURST.unpack_from(payload)
        key = (self.link.peer_session, burst_id)
        rx = self.bursts_received.get(key)
        if rx is None:
            rx = self.bursts_received[key] = BurstRx(total, first=now, last=now)
        rx.last = now
        if index in rx.seen:
            # The link must drop duplicates before they get here.
            rx.delivered_twice += 1
            self.say(now, f'WARNING: burst {burst_id} message {index} was delivered twice')
            return
        rx.seen.add(index)
        if len(rx.seen) == rx.total and not rx.done:
            rx.done = True
            self._burst_rx_summary(burst_id, rx, now, '')

    def _on_spam(self, payload: bytes, now: float) -> None:
        (counter,) = _SPAM.unpack_from(payload)
        rx = self.spam_rx
        if rx.window_received == 0 and rx.window_lost == 0:
            rx.window_start = now
        if rx.expected is not None and counter > rx.expected:
            gap = counter - rx.expected
            rx.lost += gap
            rx.window_lost += gap
        # A lower counter means a new spam run started: no loss to count.
        rx.received += 1
        rx.window_received += 1
        rx.expected = counter + 1
        rx.last = now

    # ---- summaries ---------------------------------------------------------

    def _check_summaries(self, now: float) -> None:
        link = self.link
        # A reply can take up to ttl in each direction before it is given up on.
        give_up = 2 * (link.ttl + link.rto)

        run = self.ping
        if run is not None and run.remaining == 0 and (
                not run.sent_at or now - run.last_send > give_up):
            replies = len(run.rtts)
            line = f'ping: {run.total} sent, {replies} replies, {run.total - replies} lost'
            if run.rtts:
                ms = [r * 1000 for r in run.rtts]
                line += (f', rtt min/avg/max {min(ms):.1f}/{sum(ms) / len(ms):.1f}/'
                         f'{max(ms):.1f} ms')
            self.say(now, line)
            self.ping = None

        tx = self._burst
        if tx is not None and tx.sent == tx.total and (
                link.stats()['pending'] == 0 or now - tx.last_send >= link.ttl + link.rto):
            tx.done = True
            self._burst = None
            resends = link.stats()['resent'] - tx.resent_before
            self.say(now, f'burst {tx.burst_id} sent: {tx.total} messages, {tx.acked} acked, '
                          f'{tx.failed} failed, {resends} resends, {now - tx.started:.1f} s')

        for (_, burst_id), rx in self.bursts_received.items():
            if not rx.done and now - rx.last > link.ttl + link.rto + 1.0:
                rx.done = True
                self._burst_rx_summary(burst_id, rx, now, ' (gave up waiting for the rest)')

        rx = self.spam_rx
        # Report every period, and once more when the spam stops arriving.
        if (rx.window_received or rx.window_lost) and (
                now - rx.window_start >= SPAM_REPORT_PERIOD or now - rx.last > 1.0):
            total = rx.window_received + rx.window_lost
            self.say(now, f'spam: {rx.window_received} received, {rx.window_lost} lost '
                          f'({100 * rx.window_lost / total:.1f}%) in the last '
                          f'{now - rx.window_start:.1f} s')
            rx.window_received = rx.window_lost = 0

    def _burst_rx_summary(self, burst_id: int, rx: BurstRx, now: float, note: str) -> None:
        self.say(now, f'burst {burst_id} received: {len(rx.seen)}/{rx.total}, '
                      f'{rx.total - len(rx.seen)} missing, {rx.delivered_twice} delivered '
                      f'twice, {now - rx.first:.1f} s{note}')

    def _prune(self, now: float) -> None:
        # The link reports no per-message ACK, so forget a tracked message once
        # it can no longer fail.
        if now < self._next_prune:
            return
        self._next_prune = now + 1.0
        horizon = self.link.ttl + self.link.rto + 1.0
        self._sent = {seq: entry for seq, entry in self._sent.items()
                      if now - entry[2] <= horizon}

    def _mark(self, now: float) -> None:
        if self._start is None:
            self._start = now


# ---- command line ----------------------------------------------------------

def _read_stdin(lines: 'queue.Queue[str]') -> None:
    # A thread rather than select(), because select() can't wait on stdin on Windows.
    try:
        for line in sys.stdin:
            lines.put(line.rstrip('\n'))
    except (OSError, ValueError):
        pass


def _load_script(path: Optional[str]) -> List[str]:
    if path is None:
        return []
    with open(path, encoding='utf-8') as f:
        return [line.strip() for line in f
                if line.strip() and not line.strip().startswith('#')]


def run(args: argparse.Namespace) -> int:
    """Run the tester on a UDP socket until ``quit``, Ctrl+C or ``--duration``."""
    def out(line: str) -> None:
        print(line, flush=True)

    try:
        script = _load_script(args.script)
    except OSError as exc:
        print(f'error: cannot read script: {exc}', file=sys.stderr)
        return 1

    link = Link(rto=args.rto, max_retries=args.retries, ttl=args.ttl,
                heartbeat_period=args.heartbeat, link_timeout=args.timeout)
    impairment = Impairment(args.loss, args.corrupt, random.Random(args.seed))
    tester = LinkTester(link, out, impairment=impairment, quiet=args.quiet)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer: Optional[Tuple[str, int]] = None
    try:
        if args.mode == 'listen':
            sock.bind((args.bind, args.port))
            where = f'listening on {args.bind}:{args.port}, waiting for the other end'
        else:
            peer = (socket.gethostbyname(args.host), args.port)
            sock.bind(('0.0.0.0', 0))   # Windows can't recvfrom an unbound socket
            where = f'sending to {peer[0]}:{peer[1]}'
    except OSError as exc:
        print(f'error: {exc}', file=sys.stderr)
        sock.close()
        return 1
    sock.setblocking(False)
    out(f"roverlink linktest: {where}. Session 0x{link.session:04x}, "
        f"loss {args.loss:g}, corrupt {args.corrupt:g}. Type 'help' for commands.")

    lines: 'queue.Queue[str]' = queue.Queue()
    threading.Thread(target=_read_stdin, args=(lines,), daemon=True).start()

    start = time.monotonic()
    script_resume = start
    last_error = None
    try:
        while not tester.done:
            now = time.monotonic()
            if args.duration is not None and now - start >= args.duration:
                break

            while True:
                try:
                    data, addr = sock.recvfrom(65535)
                except (BlockingIOError, InterruptedError):
                    break
                except ConnectionResetError:
                    continue   # Windows: an earlier datagram hit a closed port
                if args.mode == 'listen' and addr != peer:
                    peer = addr
                    tester.say(now, f'the other end is {addr[0]}:{addr[1]}')
                tester.receive(data, now)

            while script and now >= script_resume:
                line = script.pop(0)
                words = line.split()
                if words[0].lower() == 'sleep':
                    try:
                        script_resume = now + float(words[1])
                    except (IndexError, ValueError):
                        tester.say(now, f'bad script line {line!r}; usage: sleep S')
                else:
                    tester.command(line, now)
            while True:
                try:
                    tester.command(lines.get_nowait(), now)
                except queue.Empty:
                    break

            outgoing = tester.tick(now)
            if outgoing and peer is not None:
                for datagram in split_frames(outgoing):
                    damaged = impairment.apply(datagram)
                    if damaged is None:
                        continue
                    try:
                        sock.sendto(damaged, peer)
                        last_error = None
                    except OSError as exc:
                        # E.g. "network unreachable" while a cable is unplugged.
                        if str(exc) != last_error:
                            tester.say(now, f'send error: {exc}')
                            last_error = str(exc)

            time.sleep(min(0.01, max(0.0, link.next_wakeup() - time.monotonic())))
    except KeyboardInterrupt:
        pass
    finally:
        tester.command('stats', time.monotonic())
        sock.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``roverlink-linktest``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'UDP port (default {DEFAULT_PORT})')
    common.add_argument('--loss', type=_probability, default=0.0, metavar='P',
                        help='drop outgoing datagrams with probability P (default 0)')
    common.add_argument('--corrupt', type=_probability, default=0.0, metavar='P',
                        help='damage one byte of outgoing datagrams with probability P')
    common.add_argument('--seed', type=int, default=None,
                        help='random seed for --loss/--corrupt, to repeat a run exactly')
    # Same defaults as Link.
    common.add_argument('--rto', type=float, default=0.5,
                        help='seconds before a reliable message is resent (default 0.5)')
    common.add_argument('--retries', type=int, default=5,
                        help='resends before giving up (default 5)')
    common.add_argument('--ttl', type=float, default=5.0,
                        help='seconds before a reliable message is given up on (default 5)')
    common.add_argument('--heartbeat', type=float, default=1.0,
                        help='seconds between heartbeats (default 1)')
    common.add_argument('--timeout', type=float, default=3.0,
                        help='seconds of silence before LINK DOWN (default 3)')
    common.add_argument('--script', metavar='FILE',
                        help='run commands from FILE first (one per line, # comments)')
    common.add_argument('--duration', type=float, metavar='S',
                        help='exit after S seconds')
    common.add_argument('--quiet', action='store_true',
                        help='no per-pong or per-failure lines, only summaries and link events')

    parser = argparse.ArgumentParser(
        prog='roverlink-linktest',
        description='Test the roverlink protocol between two machines over UDP.',
        epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest='mode', required=True, metavar='{listen,connect}')
    listen = modes.add_parser('listen', parents=[common],
                              help='wait for the other end (for example on the rover)')
    listen.add_argument('--bind', default='0.0.0.0',
                        help='address to listen on (default: all interfaces)')
    connect = modes.add_parser('connect', parents=[common],
                               help='talk to a listening end (for example from the base station)')
    connect.add_argument('host', help='address of the listening end')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``roverlink-linktest`` and ``python -m roverlink.tools.linktest``."""
    return run(build_parser().parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
