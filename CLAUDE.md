# CLAUDE.md

Guidance for Claude (and humans) working in **phoenix-software**, part of the
[Domer Rover](https://github.com/Domer-Rover) org (University of Notre Dame, University Rover Challenge).

## What this repo is

Phoenix is the **radio link between the base-station computer and the rover**. It lets the team operate the
rover from a distance. Phoenix defines how messages are framed, scheduled, acknowledged and retransmitted over
two physical links:

- **RFD-900x** 900 MHz transceivers (serial, long range, low bandwidth): the primary target of the code.
- **Ubiquiti Bullet M2** 2.4 GHz bridges (IP/sockets, shorter range, high bandwidth).

The repo began as the ROS 2 stack for the old Phoenix rover. That stack is archived in `ws/src`. The live
work is `radio_comms/`, which now serves the current rover, Capybara.

## Domer Rover ecosystem

| Repo | Role | Notes |
| --- | --- | --- |
| [capybara-software](https://github.com/Domer-Rover/capybara-software) | Current URC rover; **the rover Phoenix talks to** | ROS 2 Humble, runs natively on the Jetson `jetsonson.dhcp.nd.edu`, Foxglove on `:8765` |
| [gerbil-software](https://github.com/Domer-Rover/gerbil-software) | Indoor two-wheel testbed for Capybara's nav stack | Same package layout as Capybara (`src/<robot>_bringup`, `_description`, `_hw`) |
| phoenix-software | This repo: radio comms | Plain Python, no ROS dependency in `radio_comms/` |
| [.github](https://github.com/Domer-Rover/.github) | Org profile README | |

Integration notes, to verify on hardware before you rely on them:

- **`/dev/ttyTHS1` conflict.** `radio_comms/README.md` says the rover-side RFD-900x goes on the Jetson UART
  `/dev/ttyTHS1`, but Capybara's `docker-compose.yml` maps `/dev/ttyTHS1` to its GNSS module. `roverComms.py`
  currently defaults to `/dev/ttyUSB0` (USB through FTDI). Ask before you assume which device the radio is on.
- **FTDI serial reuse.** `baseStationComms.py` hard-codes `/dev/cu.usbserial-BG00HO5R`. Capybara's
  `udev/99-rover.rules` maps that same FTDI serial (`BG00HO5R`) to `/dev/rover_roboclaw`, so the adapter was
  probably moved between devices.
- Capybara and Gerbil name devices with udev symlinks (`/dev/rover_*`, `/dev/gerbil_*`). Follow that style if
  you add a stable name for the radio.

## Repo layout

| Path | What it is |
| --- | --- |
| `radio_comms/roverlink/` | **New comms system** (pip package, stdlib only, no ROS). New work goes here. See its README |
| `radio_comms/hieroglyphics/` | Legacy comms system, superseded by `roverlink` (incompatible wire format). Keep as a reference; don't extend it |
| `radio_comms/info_over_bullets_files/` | Scrappy one-off scripts for sending data over the Bullets or Wi-Fi (testing only) |
| `radio_comms/*.py` | Scratch video tests (`simplevid.py`, `plsworkvideo.py`) |
| `radio_comms/README.md` | **Main design doc**: hardware setup, IP table, protocol, scheduler, known issues and TODOs |
| `ws/src/` | Archived ROS 2 workspace from 2024–25 (motor, MoveIt arm, ZED wrapper, rtabmap). **Read-only: do not modify unless explicitly asked** |

## roverlink (`radio_comms/roverlink`)

- `roverlink/protocol/` is a sans-IO core: `Link.feed(bytes, now)` / `Link.poll(now)` return bytes to write
  and events. It never reads the clock, starts threads or does I/O, so keep it that way; tests pass `now` in.
- Frame v1: magic `AA 55`, version, session, seq (u16, wraps), purpose, flags, number, length, payload, CRC-16.
  Reliable messages are ACKed and resent until a retry/ttl limit; stream messages are sent once.
- Planned here: routes, TCP/serial transports, a channel simulator, a fake base station. The ROS 2
  translator node lives in capybara-software as a thin package that depends on `roverlink`.

## Architecture quick reference (`radio_comms/hieroglyphics`, legacy)

- **Rover / server:** `roverComms.py`. It runs three threads: read (main thread, pushes to `MessageQueue`),
  process (`roverMessageProcessor.py`) and send (`scheduler.py`).
- **Base station / client:** `baseStationComms.py`. It runs the same three threads plus the UI
  (`userInterface.py`, text commands such as `test`, `dr`, `stop`, `ldp`, `quit`).
- **Transport:** the `ReaderWriter` interface (`readerWriter.py`) has two implementations,
  `SerialReaderWriter` and `SocketReaderWriter` (UDP). You switch between them by hand: edit the `readerWriter`
  line in each side's `main`. Automatic link switching does not exist yet.
- **Message frame** (`message.py`, big-endian):
  `ID u16 | purpose u8 | number u8 | payload size u32 | payload | checksum u8 (bytewise XOR)`.
  - `Message.Purpose` (IntEnum) is the opcode table: ACK, ERROR, MOVEMENT, HEARTBEAT, VIDEO, photos, file, …
  - IDs are unique **per side** only.
  - `number` orders chunks of multi-part payloads (images).
  - Checksums are computed but **not verified yet**.
- **Reliability:** each side ACKs every non-ACK message. The sender retransmits after about 2 s until the ID
  shows up in the scheduler's `ConcurrentSet`. Receivers drop duplicates they have already processed.
- **Scheduler:** weighted round-robin over topic queues, plus a retransmission queue.

The full rationale and the known-issues list are in `radio_comms/README.md`, which covers socket reconnects,
buffer overflow on large photos, indefinite drive commands and more. Read it before you change protocol
behavior.

## Running

```sh
# venv OUTSIDE the repo (or add it to .gitignore)
python3 -m venv ../phoenix-venv && source ../phoenix-venv/bin/activate

# roverlink
python3 -m pip install -e radio_comms/roverlink
(cd radio_comms/roverlink && python3 -m unittest discover -v) # all tests, no install needed
roverlink-linktest listen / connect <host>   # two-machine test over UDP; docs/roverlink-link-test.md

# hieroglyphics (legacy)
python3 -m pip install opencv-python numpy pyserial pygame   # pyzmq is no longer used

cd radio_comms/hieroglyphics           # imports are flat (`from message import Message`)
python3 roverComms.py                  # rover side: start FIRST
python3 baseStationComms.py            # base station: kill this before the rover side
python3 -m serial.tools.list_ports     # find the transceiver's port
```

- Serial: 57600 baud. Set `port` near the top of each driver file.
- Sockets: UDP port 9998, host `localhost` by default (second command-line argument). Real IPs are in `radio_comms/README.md`.
- `roverlink` has unit tests (stdlib `unittest`); run them before every commit that touches it.
  `hieroglyphics` has **no automated tests**, and there is **no CI**. To verify a `hieroglyphics` change, run
  `python3 -m py_compile` on the files you touched and, where possible, a localhost socket loopback (both
  sides on one machine).
- Hardware tests (radios, Bullets, Jetson) are run by a human. **Never claim a hardware test passed.** Say
  what still needs testing on hardware.

## Hardware and safety gotchas

- **Never install `libv4l-dev` on the Jetson.** It breaks hardware video encode and decode.
- URC rules require changing RFD-900x channels and frequencies through the firmware, using **RFDTools**
  (Windows, on the Domer Rover PC). This cannot be done from this code.
- RFD-900x datasheet: <https://rfdesign.com.au/wp-content/uploads/x-Series-Datasheet-V2.0.pdf>
- The IP address table lives only in `radio_comms/README.md`. Link to it; don't copy it.
- Wire-format changes must ship to **both sides at once**. A rover and base station on different protocol
  versions will silently misparse each other.

## Git workflow (hard rules)

- **NEVER `git push`, merge into `main`, run `gh pr create` / `gh pr merge`, force-push, rebase shared
  branches, or delete remote branches without explicit authorization for that specific action.** Approval
  for one push does not carry over to the next.
- **Local commits are encouraged.** Make small, focused commits as you go.
- **Every major change gets its own branch** off an up-to-date `main`, named `<type>/<kebab-slug>`:
  - `feat/*`: new capability (e.g. `feat/dynamic-link-switch`)
  - `fix/*`: bug fix
  - `docs/*`: documentation only
  - `refactor/*`: restructuring with no behavior change
  - `test/*`: tests or test tooling
  - `chore/*`: cleanup, dependencies, gitignore
  - `ci/*`: GitHub Actions
- **Commit messages** use conventional-commit subjects like the sibling repos: `feat: …`, `fix: …`,
  `docs: …`, `refactor: …`, `test: …`, `chore: …`, `ci: …`. The subject is lowercase, imperative and says
  what changed and why. Add a body when the reason isn't obvious.
- **PRs target `main`.** The `radiocomms` branch is legacy and fully merged; don't base work on it. The repo
  owner opens the PRs. When asked, Claude may draft a PR description in the sibling repos' template format
  (Description bullet list, `Fixes #…`, Testing Steps).
- **Never commit** `.vscode/`, virtualenvs, `.DS_Store`, `*~` editor backups, build artifacts, or **secrets**
  (passwords, tokens, keys). If you find a secret already in the repo, flag it to the user instead of copying
  it anywhere.

## Documentation (best effort, always considered)

Documentation matters here: the maintainers are students who rotate every year.

- Long-form guides go in a top-level `docs/` folder, matching gerbil and capybara: one topic per file,
  commands first, short prose. READMEs give an overview and link to `docs/`.
- When you edit a Python file, keep its `# Authors:` / `# Date last modified:` header current, or add one.
- New code gets docstrings and type hints. Comment the *why*, not the *what*.
- When behavior or the protocol changes, update `radio_comms/README.md` and `docs/` on the same branch. If that
  doesn't fit, say so and propose a follow-up `docs/*` branch.
- Write for the next student maintainer, who may never have seen the hardware.

## When to ask first

- Any change to the message frame, `Purpose` values or ACK semantics, since both sides must agree.
- Anything in `ws/src/`.
- Unknown hardware facts: which serial device, which antenna, current firmware settings. Ask; don't guess.
- Deleting files that look like scratch work. Someone may still use them.
