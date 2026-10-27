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

Report schema version 2 has generation time, duration, section observations,
findings, and truncation. A 15-second collection deadline, two-second subprocess
deadlines, 128 KiB subprocess output limits, and an 8 MiB report limit bound work.
An unavailable source does not invalidate the report or change its successful
exit status. JSON stdout contains exactly one document.

System command text is internal to collection. Parsers project it into typed
records before either report format sees it. No section or nested observation
contains an `output` field or a raw-text fallback. This replaces the version 1
command payloads; runtime and Go snapshot protocol versions remain 1.

| Section | Data fields |
| --- | --- |
| `interfaces` | `interfaces`: name, flags/value, MTU, options/value, link address, media, status, addresses, bridge members/configuration, optional ND6 options/value |
| `routes` | `routes`: family, native destination, canonical network, gateway, flag characters, interface, expiry seconds/marker |
| `default_route` | Destination, mask, gateway, interface, named flags, numeric metrics, route target |
| `selected_routes` | Session/VPC/range records with the same parsed route lookup observation |
| `resolvers` | `resolvers`: query scope, number, domain/search domains, nameservers, interface/index, options, flags, reachability, timeout, order, optional port |
| `network_services` | `services`: name and enabled state |
| `network_service_dns` | Service records with nameservers and an explicit configured state in nested DNS observations |
| `proxies` | Allowlisted typed `settings` and PAC configured state/scheme |
| `macos_build` | Product name/version and build version |
| `firewall` | Enabled state and numeric state |
| `packet_filter` | Enabled state, running seconds, named numeric statistics and optional rates |
| `helper_service` | Top-level launchd state, PID, runs, exit code, type, and program |
| `resolver_files` | Public receipt file metadata and typed nameserver/port/domain/search-order/timeout configuration |

Interface address records preserve the address family, prefix length, IPv6 zone
and scope ID, point-to-point peer, broadcast address, and address flags. Repeated
addresses and resolver numbers in different query scopes remain separate records.
Route findings consume these parsed records rather than interpreting command text
again. Network-service DNS collection uses the parsed enabled service list.

Command observations retain `exit_code` and `truncated` in `data`, plus
`parse_warnings` (code and 1-based line number) and `incomplete`. Complete records
survive malformed neighbours; the source remains collected with a `partial-parse`
error and `incomplete: true`. Wholly unrecognised successful output is failed with
`parse-unrecognized`. Failed commands keep their original error and exit status.
An incomplete final line of truncated command output is discarded before parsing.
Warnings never include source text. Missing values are null, not guessed defaults.

Human output uses the same parsed data in labelled tables, including helper and
runtime records and nested observations. All collected rows are displayed. Wide
records use multiple tables with a repeated row identity; narrow terminals wrap
values rather than omitting them. Empty, failed, stale, and incomplete observations
remain visible. Both formats retain the same collection and report size limits.

Local reports include network addresses and deployment identities. Explicit data
projections omit credentials, capabilities, private keys, tokens, process
arguments, arbitrary logs, and environment values. Proxy inspection removes
credentials/PAC URLs. Launchd inspection keeps only selected status fields.
Artifact inspection reads validated regular files and does not execute them or
create the binary cache. The report never performs DNS/resource probes or AWS
queries. Treat findings as observed discrepancies, not proven root causes.

Uploads and persistent diagnostic storage are outside this implementation.
