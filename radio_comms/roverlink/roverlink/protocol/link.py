# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
One end of a radio link: framing, acknowledgements, resends and liveness.

``Link`` is sans-IO. The caller owns the transport and the clock::

    link.feed(transport.read_available(), now)
    out, events = link.poll(now)
    transport.write(out)

Delivery classes:

* Reliable frames are acknowledged by the receiver and resent every ``rto``
  seconds until acknowledged, until ``max_retries`` resends, or until ``ttl``
  seconds have passed; then a ``Failed`` event is raised. The receiver drops
  duplicates with a ``DedupWindow``.
* Stream frames (drive commands, telemetry) are sent once and never
  acknowledged: a newer one replaces them, so resending would only replay
  stale data.

Reliable and stream frames use separate sequence counters. Only reliable
numbers go through the duplicate window, so heavy telemetry cannot push a
command that is still being retried out of the window.
"""

from dataclasses import dataclass
import random
import struct

from .deadline_queue import DeadlineQueue
from .dedup import DedupWindow, Verdict
from .frame import encode, FLAG_ACK, FLAG_RELIABLE, Frame, MAX_PAYLOAD, StreamDecoder
from .seq import next_seq

PURPOSE_ACK = 0
PURPOSE_HEARTBEAT = 3
RESERVED_PURPOSES = frozenset({PURPOSE_ACK, PURPOSE_HEARTBEAT})

_ACK_PAYLOAD = struct.Struct('>H')


@dataclass(frozen=True)
class Delivered:
    """A new message arrived from the peer."""

    purpose: int
    payload: bytes
    number: int = 0


@dataclass(frozen=True)
class Failed:
    """A reliable message was never acknowledged and has been given up on."""

    seq: int
    purpose: int


@dataclass(frozen=True)
class LinkUp:
    """A frame arrived after the link was down (or for the first time)."""


@dataclass(frozen=True)
class LinkDown:
    """Nothing has arrived for ``link_timeout`` seconds."""


@dataclass(frozen=True)
class PeerRestarted:
    """The peer's session changed, so it restarted."""

    old_session: int
    new_session: int


class _Pending:
    """A reliable frame waiting for its acknowledgement."""

    __slots__ = ('seq', 'purpose', 'data', 'retries', 'expires_at', 'done')

    def __init__(self, seq, purpose, data, expires_at):
        self.seq = seq
        self.purpose = purpose
        self.data = data
        self.retries = 0
        self.expires_at = expires_at
        self.done = False


class Link:
    """One endpoint of the link. See the module docstring for usage."""

    def __init__(self, session=None, rto=0.5, max_retries=5, ttl=5.0,
                 heartbeat_period=1.0, link_timeout=3.0):
        self.session = random.getrandbits(16) if session is None else session
        self.rto = rto
        self.max_retries = max_retries
        self.ttl = ttl
        self.heartbeat_period = heartbeat_period
        self.link_timeout = link_timeout

        self.peer_session = None
        self.link_up = False

        self._reliable_seq = 0
        self._stream_seq = 0
        self._out = bytearray()
        self._events = []
        self._pending = {}
        self._resends = DeadlineQueue()
        self._decoder = StreamDecoder()
        self._dedup = DedupWindow()
        self._last_rx = None
        self._next_heartbeat = None
        self._stats = dict.fromkeys(
            ('sent', 'resent', 'acked', 'failed', 'received', 'delivered',
             'duplicates', 'too_old'), 0)

    # ---- sending ----------------------------------------------------------

    def send(self, purpose, payload=b'', *, now, reliable=False, number=0):
        """Queue a message for the peer and return its sequence number."""
        if purpose in RESERVED_PURPOSES:
            raise ValueError(f'purpose {purpose} is reserved for the link')
        if len(payload) > MAX_PAYLOAD:
            raise ValueError(f'payload is {len(payload)} bytes, max is {MAX_PAYLOAD}')

        if reliable:
            seq = self._reliable_seq
            self._reliable_seq = next_seq(seq)
            data = encode(Frame(self.session, seq, purpose, FLAG_RELIABLE, number, payload))
            old = self._pending.get(seq)
            if old is not None:
                # Only possible after 65,536 reliable sends within one ttl.
                self._fail(old)
            pending = _Pending(seq, purpose, data, now + self.ttl)
            self._pending[seq] = pending
            self._resends.push(now + self.rto, pending)
        else:
            seq = self._next_stream_seq()
            data = encode(Frame(self.session, seq, purpose, 0, number, payload))

        self._out += data
        self._stats['sent'] += 1
        return seq

    def _next_stream_seq(self):
        seq = self._stream_seq
        self._stream_seq = next_seq(seq)
        return seq

    def _send_control(self, purpose, flags=0, payload=b''):
        self._out += encode(Frame(self.session, self._next_stream_seq(), purpose,
                                  flags, 0, payload))

    def _fail(self, pending):
        pending.done = True
        self._pending.pop(pending.seq, None)
        self._stats['failed'] += 1
        self._events.append(Failed(pending.seq, pending.purpose))

    # ---- receiving --------------------------------------------------------

    def feed(self, data, now):
        """Process bytes received from the transport."""
        for frame in self._decoder.feed(data):
            self._handle(frame, now)

    def _handle(self, frame, now):
        self._stats['received'] += 1
        self._last_rx = now
        if not self.link_up:
            self.link_up = True
            self._events.append(LinkUp())

        if self.peer_session is None:
            self.peer_session = frame.session
        elif frame.session != self.peer_session:
            self._events.append(PeerRestarted(self.peer_session, frame.session))
            self.peer_session = frame.session
            self._dedup.reset()

        if frame.is_ack:
            self._handle_ack(frame)
            return
        if frame.purpose == PURPOSE_HEARTBEAT:
            return

        if frame.reliable:
            # Always acknowledge, even duplicates: the sender may have missed
            # our previous ACK and will keep resending until it gets one.
            self._send_control(PURPOSE_ACK, FLAG_ACK, _ACK_PAYLOAD.pack(frame.seq))
            verdict = self._dedup.check_and_mark(frame.seq)
            if verdict is Verdict.DUPLICATE:
                self._stats['duplicates'] += 1
                return
            if verdict is Verdict.TOO_OLD:
                self._stats['too_old'] += 1
                return

        self._stats['delivered'] += 1
        self._events.append(Delivered(frame.purpose, frame.payload, frame.number))

    def _handle_ack(self, frame):
        if len(frame.payload) != _ACK_PAYLOAD.size:
            return
        (seq,) = _ACK_PAYLOAD.unpack(frame.payload)
        pending = self._pending.pop(seq, None)
        if pending is not None:
            pending.done = True
            self._stats['acked'] += 1

    # ---- timers -----------------------------------------------------------

    def poll(self, now):
        """Run due timers and return ``(bytes_to_write, events)``."""
        for pending in self._resends.pop_due(now):
            if pending.done:
                continue
            if now >= pending.expires_at or pending.retries >= self.max_retries:
                self._fail(pending)
                continue
            pending.retries += 1
            self._out += pending.data
            self._stats['resent'] += 1
            self._resends.push(now + self.rto, pending)

        if self._next_heartbeat is None or now >= self._next_heartbeat:
            self._send_control(PURPOSE_HEARTBEAT)
            if self._next_heartbeat is None:
                self._next_heartbeat = now
            # Fixed schedule, so slow polling does not make heartbeats drift;
            # after a long stall, restart the schedule from now.
            self._next_heartbeat += self.heartbeat_period
            if self._next_heartbeat <= now:
                self._next_heartbeat = now + self.heartbeat_period

        if self.link_up and now - self._last_rx >= self.link_timeout:
            self.link_up = False
            self._events.append(LinkDown())

        out, self._out = bytes(self._out), bytearray()
        events, self._events = self._events, []
        return out, events

    def next_wakeup(self):
        """Return the time ``poll`` next has work to do (may be in the past)."""
        if self._next_heartbeat is None:
            return float('-inf')
        times = [self._next_heartbeat]
        resend = self._resends.peek_deadline()
        if resend is not None:
            times.append(resend)
        if self.link_up:
            times.append(self._last_rx + self.link_timeout)
        return min(times)

    def stats(self):
        """Return counters for diagnostics and benchmarks."""
        stats = dict(self._stats)
        stats['pending'] = len(self._pending)
        stats['crc_errors'] = self._decoder.crc_errors
        stats['resyncs'] = self._decoder.resyncs
        return stats
