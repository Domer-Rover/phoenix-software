# roverlink

The radio link between the Domer Rover base station and the rover. It replaces
`../hieroglyphics/` and uses a new wire format, so the two cannot talk to each
other.

Right now this is the **protocol core**: framing, acknowledgements, resends,
duplicate filtering and link liveness. It is pure Python (standard library
only) with no ROS, threads, sockets or clock reads. You pass in the bytes you
received and the current time, and write out the bytes it returns. That makes
it run the same on the rover and on a base station laptop (Linux, macOS or
Windows), and every test runs with a fake clock.

## Install

From the repository root, in a virtual environment (Python 3.9+):

```sh
python3 -m pip install -e radio_comms/roverlink      # Linux / macOS
python -m pip install -e radio_comms\roverlink       # Windows
```

## Usage

```python
from roverlink.protocol.link import Delivered, Link

link = Link()                                  # one per process; random session id
link.send(13, b'payload', reliable=True, now=now)

# every tick (e.g. 50 Hz), with now = a monotonic time in seconds:
link.feed(transport.read_available(), now)
out, events = link.poll(now)
transport.write(out)
for event in events:
    if isinstance(event, Delivered):
        ...                                    # event.purpose, event.payload
```

`link.next_wakeup()` gives the time `poll` next has work to do, so callers can
sleep instead of spinning. `link.stats()` returns counters (sent, resent,
acked, failed, duplicates, CRC errors, ...) for diagnostics.

## Modules (`roverlink/protocol/`)

| Module | What it does |
| --- | --- |
| `frame.py` | Frame format, CRC-16, and a stream decoder that finds the next frame after corrupted bytes |
| `seq.py` | 16-bit sequence numbers that wrap at 65,535 instead of failing |
| `deadline_queue.py` | Min-heap of resend deadlines |
| `dedup.py` | Fixed-size window (1024 entries) that drops duplicate messages |
| `link.py` | One end of the link: send, receive, ACK, resend, heartbeat, link up/down |

## Frame format (v1)

All fields big-endian:

| magic | version | session | seq | purpose | flags | number | length | payload | crc |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 (`AA 55`) | 1 | 2 | 2 | 1 | 1 | 1 | 2 | ≤ 4096 | 2 |

- **magic** lets the decoder resynchronise on a byte stream: after a bad CRC,
  an unknown version or an oversized length it skips to the next `AA 55`, so
  one corrupted byte only costs the frames it touches.
- **session** is random per process start. A changed session means the peer
  restarted: the link resets its duplicate window and raises `PeerRestarted`.
- **flags**: bit 0 `RELIABLE`, bit 1 `ACK`.
- **number** is the fragment index (`0` = last or only).
- **crc** is CRC-16/CCITT-FALSE over everything after the magic.

Purposes 0 (ACK) and 3 (heartbeat) are used by the link itself; `send`
rejects them.

## Delivery classes

- **Reliable** (`reliable=True`; commands such as waypoints and mode changes):
  acknowledged by the receiver, resent every 0.5 s, and given up after 5
  resends or 5 s with a `Failed` event. The receiver drops duplicates.
- **Stream** (the default; drive commands and telemetry): sent once and never
  resent, because a newer one replaces it. A lost drive command is never
  replayed late.

The two classes use separate sequence counters, so heavy telemetry can't push
a command that is still being retried out of the duplicate window.

Heartbeats go out every 1 s. `LinkUp` fires when a frame arrives after
silence, and `LinkDown` after 3 s with nothing received. All timings are
`Link(...)` arguments.

## Tests

From this directory (no install needed):

```sh
python3 -m unittest discover -v
```

## What's next

Planned for this package: route definitions (wire format of each command and
sensor), TCP and serial transports, a lossy channel simulator, and a fake base
station CLI. The ROS 2 node that turns messages into rover topics lives in
[capybara-software](https://github.com/Domer-Rover/capybara-software) and
depends on this package.
