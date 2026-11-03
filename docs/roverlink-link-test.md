# Testing roverlink between two machines

`roverlink-linktest` runs one end of the roverlink protocol on each machine and
lets you send test traffic between them. Use it to check that the protocol
works over a real link before the ROS node, routes or serial transport exist.

It tests: framing and CRC, acknowledgements and resends, duplicate filtering,
heartbeats, link down/up detection, and peer restart detection.

It does **not** test: the RFD-900x (it runs over UDP/IP only, so the Bullets or
any network), ROS, or real rover commands.

It uses **UDP on purpose**. TCP resends lost data by itself and would hide the
protocol's own resends. UDP drops packets like a radio does.

## 1. Try it on one machine first

No hardware needed. From the repository root, in two terminals:

```sh
cd radio_comms/roverlink
python3 -m roverlink.tools.linktest listen                 # terminal 1
python3 -m roverlink.tools.linktest connect 127.0.0.1      # terminal 2
```

Both terminals print `LINK UP` within about a second. Type `send hello` in
terminal 2; terminal 1 prints `<- text 'hello'`. Type `help` for all commands
and `quit` (or Ctrl+C) to exit.

On Windows, use `python` instead of `python3`.

## 2. Two machines over the Bullets

The example below uses gerbil as the "rover" and a laptop as the base station.
Any two computers work.

1. **Wire the Bullets** as in [Bullets](../radio_comms/README.md#bullets):
   one Bullet with the laptop, the other with gerbil.
2. **Addresses.** Both machines need an address on the Bullets' network. Use
   the [IP address table](../radio_comms/README.md#ip-addresses) for the
   laptop. Gerbil is not in the table: give it a free address in the same
   network (not one in the table), for example on Linux:
   ```sh
   ip -br addr                                          # find the Ethernet interface
   sudo ip addr add 192.168.11.<free>/24 dev <interface>   # lasts until reboot
   ```
3. **Check the network** before anything else, from each machine:
   ```sh
   ping <other machine's address>
   ```
   If ping fails, fix the wiring or addresses first; the test tool can't help.
4. **Get the code onto gerbil.** Python 3.9 or newer, nothing to install.
   Either check out the branch there, or copy the folder from the laptop:
   ```sh
   scp -r radio_comms/roverlink <user>@<gerbil-address>:~/roverlink
   ```
5. **Firewall.** Allow UDP port 5005 (`sudo ufw allow 5005/udp` if `ufw` is
   on; on Windows, accept the firewall prompt the first time).
6. **Run it:**
   ```sh
   # on gerbil
   cd ~/roverlink && python3 -m roverlink.tools.linktest listen
   # on the laptop
   cd radio_comms/roverlink && python3 -m roverlink.tools.linktest connect <gerbil-address>
   ```
   Both ends print `LINK UP`. The listening end also prints the laptop's address.

Optional: `python3 -m pip install -e radio_comms/roverlink` puts a
`roverlink-linktest` command on your PATH, so you can type
`roverlink-linktest listen` from anywhere.

## Commands

Type these into either end while it runs.

| Command | What it does |
| --- | --- |
| `send TEXT` | Reliable text message: acknowledged, resent if lost |
| `stream TEXT` | Stream text message: sent once, never resent |
| `ping [N]` | N reliable pings (default 1), then round-trip time min/avg/max |
| `burst N [SIZE]` | N reliable messages of SIZE bytes (default 32). Both ends print a summary when done |
| `spam HZ [SIZE]` | Stream messages at HZ per second until `stop`. The other end reports how many were lost every 5 s |
| `stop` | Stop `spam` |
| `loss P` | Drop outgoing datagrams with probability P (0 to 1) |
| `corrupt P` | Damage one byte of outgoing datagrams with probability P |
| `status` | Link up/down, both session ids, unacknowledged messages |
| `stats` | Protocol counters (sent, resent, acked, failed, duplicates, CRC errors, ...) |
| `help`, `quit` | |

Options (put them after `listen` or `connect HOST`):

| Option | Default | Meaning |
| --- | --- | --- |
| `--port` | 5005 | UDP port; must match on both ends |
| `--loss P`, `--corrupt P` | 0 | Simulated damage on what this end sends |
| `--seed N` | random | Makes `--loss`/`--corrupt` repeatable |
| `--rto`, `--retries`, `--ttl` | 0.5 s, 5, 5 s | Resend interval, resend limit, give-up time |
| `--heartbeat`, `--timeout` | 1 s, 3 s | Heartbeat interval, silence before `LINK DOWN` |
| `--script FILE` | | Run commands from a file first (one per line, `# comments`, `sleep S`) |
| `--duration S` | | Exit after S seconds |
| `--quiet` | | Only summaries and link events, no per-pong or per-failure lines |

## Test checklist

Run these in order on the real link. "Laptop" is the `connect` end.

| # | Do | Expect |
| --- | --- | --- |
| 1 | Start both ends | `LINK UP` on both |
| 2 | Laptop: `send hello` | Gerbil prints `<- text 'hello'` exactly once |
| 3 | Laptop: `ping 20` | `ping: 20 sent, 20 replies, 0 lost` and round-trip times |
| 4 | Laptop: `burst 500` | Laptop: `500 acked, 0 failed`. Gerbil: `500/500, 0 missing, 0 delivered twice` |
| 5 | Laptop: `loss 0.3`, then `burst 500` | Laptop shows resends. Every message is acked or reported failed. Gerbil: `0 delivered twice`, and its missing count is never more than the laptop's failed count. Then `loss 0` |
| 6 | Laptop: `corrupt 0.3`, then `burst 500`, then `stats` on gerbil | Gerbil's `crc_errors` goes up and the burst still completes with 0 delivered twice. Then `corrupt 0` |
| 7 | Laptop: `spam 20`, wait 10 s, `stop` | Gerbil reports received/lost every 5 s. Laptop `stats`: `resent` did not go up (stream messages are never resent) |
| 8 | Unplug one Bullet's Ethernet, then laptop: `send lost?` | `LINK DOWN` on both ends about 3 s after unplugging. The message prints `FAILED` about 3 s after it was sent (5 resends, 0.5 s apart) |
| 9 | Plug it back in | `LINK UP` again; `send hello` works without restarting anything |
| 10 | Ctrl+C the laptop end and start it again | Gerbil prints `PEER RESTARTED` and the new laptop's messages arrive |
| 11 | Swap roles (laptop `listen`, gerbil `connect`) and repeat 2–4 | Same results |

For step 5, `loss` damages only what the laptop sends. Start gerbil with
`--loss 0.3` too to damage both directions.

### What the numbers mean

- **acked + failed = sent** for a burst. A message counts as failed when it was
  not acknowledged after 5 resends (about 3 s) or 5 s, whichever comes first.
- **Failed doesn't always mean lost.** If the message arrived but every
  acknowledgement was lost, the receiver has it and the sender reports it failed.
  That is why the receiver's missing count can be lower than the failed count,
  but never higher.
- **Delivered twice must always be 0.** Anything else is a protocol bug: save
  both terminal outputs and report it.
- **Ping round-trip times** include resends, so under loss some pings take
  500 ms or more (one `--rto`).

### Results

Fill this in when you run the checklist on hardware.

| Date | Link | Ends | Steps passed | Ping avg | Notes |
| --- | --- | --- | --- | --- | --- |
| | Bullets | laptop ↔ gerbil | | | |

## Repeatable runs

Put commands in a file and run them with `--script`, so every test run is the same:

```sh
cat > burst-test.txt <<'EOF'
# wait for the link, then test
sleep 1
ping 10
sleep 3
burst 1000
EOF
python3 -m roverlink.tools.linktest connect <address> --script burst-test.txt --duration 30 --quiet
```

`--seed` with `--loss`/`--corrupt` repeats the same simulated damage. Real
radio loss, of course, is different every run.

## Troubleshooting

- **No `LINK UP`:** check `ping` both ways, the address given to `connect`, that
  both ends use the same `--port`, and the firewall. Only the `connect` end
  knows where to send, so the listening end stays silent until it hears from it.
- **`error: [Errno 98] Address already in use`:** another listener is still
  running on that port. Stop it or use `--port`.
- **`send error: ...`** (for example `Network is unreachable`): the local
  Ethernet cable or address went away. The tool keeps running and recovers when
  it comes back. Often there is no error at all, because UDP doesn't know the
  other end is gone; `LINK DOWN` is the reliable signal.
- **`ModuleNotFoundError: No module named 'roverlink'`:** run from the
  `radio_comms/roverlink` folder, or `pip install -e` it.
