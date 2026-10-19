# Contributing to Traforo Go

Keep protocol validation bounded and independent of Darwin APIs. Keep native
system calls behind the Darwin adapter. The Darwin-only helper implementation
lives in `internal/helper/darwin`; `internal/helper` is the portable dispatcher.
The Darwin helper owns authorization, session capabilities, route and resolver
lifecycle, durable recovery, and installation receipts. Keep raw utun descriptors
inside the helper. Treat a resource with uncertain ownership as evidence to
retain, not as permission to delete it.

Do not add a forwarder command or public tunnel API before its assigned stack
step. Package initialization must not install a service or change host networking.

Use the shared vectors in `internal/helper/testdata/independent_vectors.json` as
independent test data. Do not regenerate expected bytes with the encoder under
test. Preserve the vector format for later Go and Python consumers.

From this directory, run `make test`, `make race`, `make vet`, and `make lint`.
Run `make fuzz` for the bounded request decoder. Run `make fuzz-snapshot` for
the snapshot decoder on macOS. Run `make format` before you commit Go source
changes. Darwin tests require macOS, cgo, and the Apple SDK. Tests must not
install or uninstall the helper or mutate host routes and DNS settings. Actual
host recovery acceptance belongs to the authorized native integration step.

## Destination ranges

`Request.Ranges` authorizes routed IPv4 destinations for one unit. It does not
describe VPC ownership or automatically authorize the rest of a VPC. The deployment
contract selects private and isolated subnets; public subnet and unused VPC
addresses receive no implicit authorization. The upstream VPC DNS resolver uses
its separate SOCKS path and need not belong to these destination ranges.

The bounded request allows 1–32 disjoint canonical RFC1918 IPv4 prefixes, from
`/16` through `/28`. The current ten-AZ layout needs at most twenty private and
isolated subnet ranges. Duplicate and overlapping ranges are invalid. The count
remains one byte and the `STLVTUN1` layout is unchanged. A maximum configuration
with 32 longest prefixes and 64 longest resolver names has a 17,020-byte body,
below the 32,768-byte limit. Existing independent wire vectors remain unchanged.

The Python encoder must use the same range limit when its administration layer is
updated. Helper journal capacity and lifecycle checks belong to the helper layer;
forwarder and DNS policy checks belong to their packages. This protocol layer does
not install routes or infer subnet classification from an address.
