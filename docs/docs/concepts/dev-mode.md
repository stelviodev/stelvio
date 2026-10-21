# `stlv dev`: run Lambda functions locally

`stlv dev` is like `stlv deploy`, but your Lambda handler code runs on your machine while the rest of your infrastructure stays in AWS.

## Why you'd use it

- No redeploy on every code change
- See `print()` output immediately
- Exceptions show up in your terminal (and in the Lambda response)
- Easy to attach a local debugger

## How it works

When you run `stlv dev`, Stelvio deploys your app in **dev mode**:

- Your Lambdas get replaced with a small stub Lambda.
- The stub forwards each invocation over an AppSync Events channel.
- The `stlv dev` process runs a local dev server, executes your real handler, then sends the result back to AWS.
- The bridge checks AppSync connection and subscription acknowledgements before it accepts invocations.

Your public entrypoint (API Gateway URL / Function URL) stays the same — you hit it like normal.

### Managed VPC transport

For a VPC with an enabled bastion, Stelvio uses the deployed networking metadata
to identify the AWS account, provider, region, VPC, and owned access instance.
It checks the complete VPC CIDR and subnet inventory and requires VPC DNS support
and hostnames to be enabled before using private DNS. Handler changes to AWS
environment variables cannot redirect the captured AWS session.

VPC CIDRs describe AWS ownership. Inventory includes the primary block and all
associated IPv4 blocks, and verifies every declared public, private, and isolated
subnet by ID, account, VPC, Availability Zone, CIDR, and availability. Missing,
duplicate, or changed records are refused. Subnet tiers come from the deployment
metadata, not API response order or public IPv4 auto-assignment settings.

Only the declared private and isolated subnet CIDRs are eligible resource routes.
Public subnets, unused VPC space, and newly associated blocks do not implicitly
add destinations. The VPC resolver remains the primary block's address plus two;
it uses the separate SOCKS path and need not be inside a routed subnet. Full VPC
ownership overlap remains an error even when the selected subnet routes differ.
The AWS inventory check does not inspect route tables to reclassify subnets.

The access path uses Systems Manager and EC2 Instance Connect; it does not open
inbound SSH. OpenSSH learns the bastion's Ed25519 host key through the verified
SSM connection on first use. After SSH authentication succeeds, Stelvio stores
that key by AWS partition, account, region, and instance ID. It requires the same
key on reconnect and across command restarts. A changed key stops the connection;
it is never accepted by automatic recovery.

Pins are private, per-user records under Stelvio's binary cache in
`ssh-host-trust`. Session cleanup and helper uninstall leave these records intact.
An instance replacement gets a new identity and a new pin. Intentional key rotation
on the same instance requires an explicit trust reset; see
[host-key recovery](../intro/troubleshooting.md#managed-vpc-transport-identity-and-authorization).

OpenSSH uses the verified Session Manager plugin at its
private cache path. A missing or unsafe plugin is a local artifact error; repair
it with `stlv tunnel install` rather than retrying the AWS connection.

For DocumentDB, discovery checks the cluster ARN, VPC, security groups, subnet
group, port, and current member list before adding cluster, reader, and member
hostnames to that VPC's private DNS view. Changed ownership or host identity is
refused. AWS service or SSM availability issues can be retried after the deployed
resources and permissions are checked.

### Session ownership and readiness

Before local handler code runs, Stelvio reads the deployed network metadata and
captures the selected AWS provider, account, region, profile, and startup
environment. It starts networking in a separate nonroot Python process with that
captured environment. Later handler changes to AWS variables cannot redirect the
session's clients. The parent and runtime exchange bounded messages over a private
local socket; an independent lease pipe lets the runtime detect parent exit.
The session metadata preserves the VPC ownership ranges and each public, private,
and isolated subnet's ID, availability zone, and CIDR across that process boundary.

Each eligible VPC has its own worker, transport attempt, helper generation, DNS
revision, discovery state, and retry schedule. A failure in one VPC leaves workers
for other VPCs running. The worker rejects that VPC's private DNS while its
transport is down and keeps owned resolver claims and the DNS listener port stable
while it reconnects to the existing bastion. It does not replace the bastion.
The worker freezes the deployed private and isolated subnet ranges for the session.
The forwarder and privileged helper use that same destination set. Reconnection,
member discovery, and recovery of a lost helper reply cannot widen it to a whole
VPC block. An empty destination set or a changed subnet selection fails before
SSH starts; there is no public-subnet or whole-VPC fallback.

If an existing LAN or VPN route overlaps a selected destination, the VPC fails
closed. Status includes the helper's configure stage, requested range, conflicting
range, and interface index when the helper supplies those facts. Inspect the
conflict and restart after resolving it. Other ready VPCs and unrelated handlers
can continue. Cleanup uncertainty retains the conflict evidence; use
`stlv tunnel status` and `stlv tunnel cleanup` to inspect and retry owned cleanup.
Do not delete foreign routes or ownership journals to bypass a refusal.

A changed SSH host key is a terminal failure for that VPC. Status reports
`cause=ssh-host-key`, the instance identity, and the expected and presented
fingerprints. The worker stops reconnecting and keeps its existing DNS view
rejecting queries. Dependent invocations remain blocked; other ready VPCs and
unrelated handlers can continue. Restoring the original key requires a new
network session. For intentional rotation, stop the affected tunnels, explicitly
reset that instance's trust, and restart. A new runtime process reads the same
persistent pin; restarting alone does not accept a changed key.

`READY` means the scoped resolver answered through the VPC path, the helper's
routes and resolver files are active, the transport and forwarder are healthy,
and each discovered resource hostname resolved to an address inside the selected
private or isolated subnets and accepted a TCP connection on its declared port
through both SOCKS and ordinary host routing. Resolver queries use TCP through
SOCKS to the owning VPC's primary resolver; that address need not be routed through
the virtual interface. A separate system-resolver observation proves macOS scoped
DNS delivery. A resolver-only direct probe cannot prove resource routing and is
not used. A working SOCKS proxy by itself
does not make a VPC ready. New invocations that depend on an unready VPC are
rejected before handler execution; unrelated VPCs and handlers without a VPC can
continue.

With no enabled VPC dependencies, Stelvio skips helper activation. A VPC whose
bastion policy is disabled is reported and left to the developer's local network.
On normal shutdown, each worker stops its owned SSH/SSM transport and retires its
helper generation before the lease is released. If the parent exits unexpectedly,
the lease closes and the helper recovers host state from its durable journal. A
transport cleanup that cannot be certified leaves its ownership receipts and
helper evidence available through `stlv tunnel status`; Stelvio reports the
existing recovery commands instead of claiming the VPC is ready. This status is
local helper ownership state and does not verify AWS or database connectivity.

## Using it

No changes to `stlv_app.py` are needed.

```python
@app.run
def run() -> None:
    api = RestApi("MyApi")
    api.route("get", "/", "functions/api.handler")
```

Start dev mode:

```bash
stlv dev            # uses your personal environment
stlv dev staging    # explicit environment
```

Now call your API like you normally would (e.g. `https://...execute-api.../v1/`). Every request loads your handler and the files it imports fresh, so an edit shows on the next request. Installed packages stay loaded.

To stop the local server: `Ctrl+C`. The bridge closes its AppSync connection during shutdown. If a local handler is still running, the bridge does not wait indefinitely for its worker thread.

!!! warning
    Stopping the dev server won't change the infrastructure in AWS. In particular, the replacement Lambda function that handles the dev mode ("stub Lambda") will remain deployed.

    You need to re-deploy without dev mode using `stlv deploy` again to switch back to your real Lambda code being deployed to AWS.

!!! warning
    `stlv dev` still deploys real AWS resources (so it needs AWS credentials and it can cost money). It will also create an AppSync API named `stelvio` in your account if it's not already present. The AppSync API is a purely usage-based cost model.


## Limitations

### Target Architecture

Stelvio's dev mode will execute your local Lambda functions natively, i.e. the Python interpreter is used on your workstation's operating system and CPU architecture, no matter your Lambda function definition. There is no emulator in place that would match the os/architecture on AWS with the one on your workstation.

Since your workstation is likely equipped with more RAM than your Lambda function, this also mean that you're less likely to run into memory limits on your workstation.

### Request/Response Size Limits

Dev mode supports the same payload limits as [AWS Lambda](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) (internally, large payloads are chunked into multiple AppSync events and reassembled on the other end):

- **6 MB** for request and response (synchronous invocations)
- **1 MB** for asynchronous invocations
