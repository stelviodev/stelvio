# Dev VPC Phase 3 evidence

Date: 2 October 2026. Host: macOS darwin 24.6.0. Python 3.12.11. No AWS resources were created. No sudo. No live pf, no live `/etc/resolver`, no live Linux, and no kernel `DIOCNATLOOK` or `SO_ORIGINAL_DST`.

Phase 3 wires dev-session lifecycle and invocation admission. It does not prove private hosted-zone answers, a live packet filter, or the Phase 2 OS-integration cases. `rules_applied: false` means the packet filter did not run. A child PID is not readiness. A01 and A02 stay unmet.

## What was run

`uv run ruff check` on the Phase 3 Python files passed.

```
uv run pytest tests/dev tests/bridge/test_stub.py tests/bridge/test_dev_server.py \
  tests/bridge/test_dispatch.py tests/aws/function/test_function_dev_mode.py \
  tests/aws/function/test_function_vpc.py -q
```

190 passed.

Those tests cover mode selection (unused VPCs ignored; two used VPCs rejected only for managed), admission from `StatusBus` before ready and during reconnect, queue expiry and a capacity of 32, old and new stub envelopes, a workstation clock that does not extend a remaining-budget deadline, timeout ownership until the handler thread returns, non-VPC handlers while the tunnel is reconnecting, independent bridge and network reconnect with no replay, metadata read from an in-memory state blob before simulated workdir cleanup, and double-stop.

The default managed connector raises `network_not_ready` and does not open SSH. The default helper applicator still returns `applied: false` and does not call `pfctl`. Tests that reach READY inject a helper double. That double is not a live packet filter.

## Acceptance

| Case | Verdict | Why |
| --- | --- | --- |
| A05 | BLOCKED | Unit tests show the supervisor is not stopped by admission or by an AppSync reconnect, and READY requires `rules_applied`, not a child PID. Reload, idle, a long handler, and a debugger pause were not run against a live network subprocess. |
| A06 | BLOCKED | Unit tests close readiness on transport loss and name the status code. Live SSH/plugin kill, credential expiry, session-duration exhaustion, and workstation-network interruption were not run. No fresh authenticated recovery was executed. |
| A07 | PASS | Unit tests reject events before readiness, during recovery, after the monotonic queue budget, and past 32 queued events. A far-future Lambda epoch does not extend the budget. Reconnect does not replay a handler or emit a second response for the same invocation id. |
| A12 | PASS for the unit regression | A non-VPC endpoint runs while the managed tunnel is reconnecting. The credential snapshot is taken before a handler's temporary environment and does not copy static access keys into the child environment. External mode starts no tunnel and does not change routes or DNS. A live external VPN was not used. |
| A14 | PASS | A timed-out executor keeps the environment and the admission lock until the thread returns. The next handler does not overlap it. The late result is logged as outlived and is not returned. `stop` does not wait for that thread and says the handler was not cancelled. |

## Whether Phase 4 may start

Phase 4 public docs and the changelog were not written. They may describe this lifecycle and admission contract. They must not say the plan's Phase 3 exit is fully met, and they must not claim any of these:

- A01 or A02
- private hosted-zone answers
- live pf, live `/etc/resolver`, or live Linux
- a real `DIOCNATLOOK` or `SO_ORIGINAL_DST`
- `rules_applied: true` from the default applicator as a live packet filter
- the live halves of A05 and A06 (reload, idle, debugger pause, SSH and credential fault injection)
- a live external VPN
