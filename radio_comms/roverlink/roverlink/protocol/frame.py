# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""
Frame format and a byte-stream decoder.

One frame on the wire (all fields big-endian)::

    magic    2  0xAA 0x55
    version  1  FRAME_VERSION
    session  2  random per process start
    seq      2  0..65535, wraps
    purpose  1  what the payload is
    flags    1  FLAG_RELIABLE, FLAG_ACK
    number   1  fragment index (0 = last or only)
    length   2  payload size, at most MAX_PAYLOAD
    payload  n
    crc      2  CRC-16/CCITT-FALSE over everything after magic

The magic marker lets the decoder find the next frame after corrupted bytes,
which matters on a serial radio where bytes can be lost or flipped.
"""

from dataclasses import dataclass
import struct

MAGIC = b'\xaa\x55'
FRAME_VERSION = 1
MAX_PAYLOAD = 4096

FLAG_RELIABLE = 0x01
FLAG_ACK = 0x02

_HEADER = struct.Struct('>2sBHHBBBH')
_CRC = struct.Struct('>H')
HEADER_SIZE = _HEADER.size
CRC_SIZE = _CRC.size
OVERHEAD = HEADER_SIZE + CRC_SIZE


def crc16_ccitt(data, crc=0xFFFF):
    """Return the CRC-16/CCITT-FALSE checksum of ``data``."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Frame:
    """One decoded frame."""

    session: int
    seq: int
    purpose: int
    flags: int = 0
    number: int = 0
    payload: bytes = b''

    @property
    def reliable(self):
        """Return True if the sender expects an acknowledgement."""
        return bool(self.flags & FLAG_RELIABLE)

    @property
    def is_ack(self):
        """Return True if this frame acknowledges another frame."""
        return bool(self.flags & FLAG_ACK)


def encode(frame):
    """Serialise ``frame`` to bytes, ready to write to a transport."""
    if len(frame.payload) > MAX_PAYLOAD:
        raise ValueError(
            f'payload is {len(frame.payload)} bytes, max is {MAX_PAYLOAD}')
    header = _HEADER.pack(
        MAGIC, FRAME_VERSION, frame.session, frame.seq, frame.purpose,
        frame.flags, frame.number, len(frame.payload))
    body = header[len(MAGIC):] + frame.payload
    return MAGIC + body + _CRC.pack(crc16_ccitt(body))


class StreamDecoder:
    """
    Turn a byte stream into frames.

    ``feed`` accepts bytes in any chunking: partial frames are kept until the
    rest arrives. On a bad checksum, an unknown version or an oversized length
    the decoder drops one byte and searches for the next magic marker, so one
    corrupted byte costs at most the frames it touches.
    """

    def __init__(self):
        self._buf = bytearray()
        self.crc_errors = 0
        self.resyncs = 0

    def feed(self, data):
        """Add received bytes and return every complete frame found."""
        self._buf += data
        frames = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _next_frame(self):
        buf = self._buf
        while True:
            start = buf.find(MAGIC)
            if start < 0:
                # Keep a trailing first magic byte: the second may still arrive.
                keep = 1 if buf[-1:] == MAGIC[:1] else 0
                if len(buf) > keep:
                    self.resyncs += 1
                    del buf[:len(buf) - keep]
                return None
            if start > 0:
                self.resyncs += 1
                del buf[:start]
            if len(buf) < HEADER_SIZE:
                return None

            _, version, session, seq, purpose, flags, number, length = \
                _HEADER.unpack_from(buf)
            if version != FRAME_VERSION or length > MAX_PAYLOAD:
                self._skip_byte()
                continue

            end = HEADER_SIZE + length
            if len(buf) < end + CRC_SIZE:
                return None
            (crc,) = _CRC.unpack_from(buf, end)
            if crc != crc16_ccitt(bytes(buf[len(MAGIC):end])):
                self.crc_errors += 1
                self._skip_byte()
                continue

            payload = bytes(buf[HEADER_SIZE:end])
            del buf[:end + CRC_SIZE]
            return Frame(session, seq, purpose, flags, number, payload)

    def _skip_byte(self):
        self.resyncs += 1
        del self._buf[:1]
