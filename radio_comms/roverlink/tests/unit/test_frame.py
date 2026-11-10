# Authors: Gabriel da Silva Marques
# Date last modified: November 3, 2026

"""Tests for frame encoding and the stream decoder."""

import random
import unittest

from roverlink.protocol.frame import (
    crc16_ccitt, encode, FLAG_ACK, FLAG_RELIABLE, Frame, HEADER_SIZE,
    MAX_PAYLOAD, StreamDecoder)


def sample(seq=1, payload=b'hello', flags=FLAG_RELIABLE):
    return Frame(session=0xBEEF, seq=seq, purpose=2, flags=flags,
                 number=0, payload=payload)


class TestEncode(unittest.TestCase):

    def test_crc_known_value(self):
        # Standard check value for CRC-16/CCITT-FALSE
        self.assertEqual(crc16_ccitt(b'123456789'), 0x29B1)

    def test_round_trip(self):
        frame = sample()
        self.assertEqual(StreamDecoder().feed(encode(frame)), [frame])

    def test_empty_and_max_payload(self):
        for payload in (b'', bytes(MAX_PAYLOAD)):
            frame = sample(payload=payload)
            self.assertEqual(StreamDecoder().feed(encode(frame)), [frame])

    def test_oversized_payload_rejected(self):
        with self.assertRaises(ValueError):
            encode(sample(payload=bytes(MAX_PAYLOAD + 1)))

    def test_flags(self):
        self.assertTrue(sample(flags=FLAG_RELIABLE).reliable)
        self.assertFalse(sample(flags=0).reliable)
        self.assertTrue(sample(flags=FLAG_ACK).is_ack)


class TestStreamDecoder(unittest.TestCase):

    def test_byte_by_byte(self):
        frame = sample()
        decoder = StreamDecoder()
        out = []
        for byte in encode(frame):
            out += decoder.feed(bytes([byte]))
        self.assertEqual(out, [frame])

    def test_concatenated_frames(self):
        frames = [sample(seq=i, payload=bytes([i]) * i) for i in range(10)]
        data = b''.join(encode(f) for f in frames)
        self.assertEqual(StreamDecoder().feed(data), frames)

    def test_random_chunking(self):
        rng = random.Random(1)
        frames = [sample(seq=i, payload=rng.randbytes(rng.randrange(50)))
                  for i in range(50)]
        data = b''.join(encode(f) for f in frames)
        decoder = StreamDecoder()
        out = []
        i = 0
        while i < len(data):
            n = rng.randrange(1, 40)
            out += decoder.feed(data[i:i + n])
            i += n
        self.assertEqual(out, frames)

    def test_garbage_prefix(self):
        frame = sample()
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(b'\x00\xaa\x13garbage' + encode(frame)), [frame])
        self.assertGreater(decoder.resyncs, 0)

    def test_bad_crc_resyncs_to_next_frame(self):
        first, second = sample(seq=1), sample(seq=2)
        broken = bytearray(encode(first))
        broken[HEADER_SIZE] ^= 0xFF  # corrupt one payload byte
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(bytes(broken) + encode(second)), [second])
        self.assertEqual(decoder.crc_errors, 1)

    def test_oversized_length_resyncs(self):
        good = sample(seq=2)
        broken = bytearray(encode(sample(seq=1)))
        broken[HEADER_SIZE - 2:HEADER_SIZE] = (MAX_PAYLOAD + 1).to_bytes(2, 'big')
        self.assertEqual(StreamDecoder().feed(bytes(broken) + encode(good)), [good])

    def test_wrong_version_resyncs(self):
        good = sample(seq=2)
        broken = bytearray(encode(sample(seq=1)))
        broken[2] = 99
        self.assertEqual(StreamDecoder().feed(bytes(broken) + encode(good)), [good])

    def test_partial_magic_kept(self):
        frame = sample()
        data = encode(frame)
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(b'junk' + data[:1]), [])
        self.assertEqual(decoder.feed(data[1:]), [frame])

    def test_random_corruption_never_raises(self):
        rng = random.Random(2)
        frames = [sample(seq=i, payload=rng.randbytes(20)) for i in range(200)]
        data = bytearray(b''.join(encode(f) for f in frames))
        for _ in range(100):
            data[rng.randrange(len(data))] = rng.randrange(256)
        out = StreamDecoder().feed(bytes(data))
        # Every frame that comes out is one that went in
        self.assertTrue(set(out) <= set(frames))
        self.assertGreater(len(out), 100)


if __name__ == '__main__':
    unittest.main()
