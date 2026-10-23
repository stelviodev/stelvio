# Shared tunnel diagnostics

This package supplies data for local reports and future diagnostic clients. It
has no Click, Rich, Textual, AWS, or deployment dependency in its IPC layer.
`report.py` adds macOS collectors and Rich rendering.

## Interfaces

- `runtime.discover()` returns safe, current-UID candidate socket paths. A failed
  connection identifies a dead endpoint; discovery never removes its files.
- `runtime.snapshot(path, after_sequence=0)` returns a fresh detached dictionary.
  Requests are read-only and cannot acquire a lease or change networking.
- `NativeHelper(timeout=0.5).diagnostics()` returns authorized helper state.
- `Forwarding.diagnostics()` uses a separate 500 ms statistics exchange. A failed
  exchange closes statistics only; mutation control remains available.

Snapshot protocol version 1 uses a four-byte big-endian JSON length. The request
is `{ "version": 1, "operation": "snapshot", "after_sequence": 0 }`. Runtime
messages are limited to 1 MiB; Go snapshots are limited to 256 KiB. Unexpected
fields, descriptors, duplicate keys, unsupported versions, and mutations are
rejected. Runtime clients authenticate each other as the same ordinary UID.

Runtime snapshots contain `version`, `epoch`, `sample_monotonic_ns`, `session`,
`sources`, and `events`. A source contains `status`, `observed_at`, `age_seconds`,
`stale`, `error`, and `data`. Failed reads retain the last successful data with its
age. Five seconds makes a source stale. Unavailable values are null, not zero.
Source snapshots are not atomic across processes: compare epoch, session, unit,
generation, attempt, and DNS revision before combining observations.

Events have a sequence, UTC timestamp, code, unit, and bounded detail. Replies
include oldest/newest sequences and a gap flag. Consumers pass the last sequence
they consumed. Event history ends when the runtime exits.

The runtime keeps no idle collector. Readers share at most one sample per source
per second. Closing a client never changes tunnel ownership. Endpoint setup or
collection failure must not affect admission, forwarding, or cleanup.

## Counter meaning

Helper counters measure inner IPv4 bytes without carrier or utun framing.
Received and delivered packets/bytes are separate. Already-consumed packets that
cannot be delivered count as drops; interrupted reads that consume nothing do
not. Unattributable frames remain a process-level count. These are not AWS billing
bytes and omit SSH/SSM overhead and DNS's direct SOCKS path.

Forwarder TCP counters cover admitted connection workers, including setup.
Completion requires both copy directions to end without error. Refused workers
never increment opened/active. Queue occupancy is a gauge; queue drops are
cumulative. DNS response counts use the numeric response code index. Upstream
exchange duration totals/maxima use nanoseconds and include failed completed
exchanges. Queries in progress are gauges.

Counters live in memory. Helper totals survive reconnects within a session;
forwarder TCP/DNS totals survive stack/relay replacement for one logical unit.
A new process or session resets the identity. Gauges describe current work.
A future monitor must reset rate baselines after epoch/session changes or counter
regression and calculate rates from monotonic sample-time differences. This
change does not implement rates, chart history, or a monitor command.

## Reports and privacy

Report schema version 1 has generation time, duration, section observations,
findings, and truncation. A 15-second collection deadline, two-second subprocess
deadlines, 128 KiB subprocess output limits, and an 8 MiB report limit bound work.
An unavailable source does not invalidate the report or change its successful
exit status. JSON stdout contains exactly one document.

Local reports include network addresses and deployment identities. Explicit data
projections omit credentials, capabilities, private keys, tokens, process
arguments, arbitrary logs, and environment values. Proxy inspection removes
credentials/PAC URLs. Launchd inspection keeps only selected status fields.
Artifact inspection reads validated regular files and does not execute them or
create the binary cache. The report never performs DNS/resource probes or AWS
queries. Treat findings as observed discrepancies, not proven root causes.

Uploads and persistent diagnostic storage are outside this implementation.
