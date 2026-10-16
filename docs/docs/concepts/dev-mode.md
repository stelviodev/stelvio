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

Your public entrypoint (API Gateway URL / Function URL) stays the same. You hit it like normal.

## Using it

No changes to `stlv_app.py` are needed.

```python
from stelvio.aws.api_gateway import RestApi

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

To stop the local server: `Ctrl+C`. 

!!! warning
    Stopping the dev server leaves your application infrastructure in AWS, including the replacement stub Lambda. Temporary VPC access is cleaned up separately; persistent access stays deployed. See [VPC access policies](../components/aws/vpc.md#dev-access-policy).

    You need to re-deploy without dev mode using `stlv deploy` again to switch back to your real Lambda code being deployed to AWS.

!!! warning
    `stlv dev` still deploys real AWS resources (so it needs AWS credentials and it can cost money). It will also create an AppSync API named `stelvio` in your account if it's not already present. The AppSync API is a purely usage-based cost model.


## Accessing a VPC from your local handler

On macOS, `stlv dev` can connect your local handlers to private IPv4 TCP services,
including DocumentDB. Keep the same `Function(vpc=...)`, linked hostname, port,
and driver TLS settings you use in production. You do not need a localhost URI
or a separate handler for dev mode.

### Setup

1. Use macOS 15 or newer on Apple Silicon or Intel. The wheel includes the
   precompiled **Stelvio Traforo** executable for both architectures. You do not
   need Go or Xcode. Linux, WSL, and Windows VPC networking are not implemented;
   ordinary dev mode without managed VPC access still works on those platforms.
2. Configure your AWS profile or normal SDK credential chain. Those credentials
   need Systems Manager, EC2 Instance Connect, resource discovery, and access to
   any secrets your local handler reads. Custom Pulumi-provider credentials,
   assume-role settings, endpoints, and proxies are not supported for tunnels.
   `stlv tunnel install` downloads a pinned AWS Session Manager plugin into
   Stelvio's private `bin` directory alongside Pulumi. No manual plugin install,
   Homebrew, or PATH configuration is required.
3. Install your handler's dependencies in the Python environment running `stlv`.
   For DocumentDB, install `pymongo` there as well as declaring it in the
   Function's `requirements`. Lambda packaging does not install local packages.
4. Install the helper and private plugin from that Stelvio environment:

    ```bash
    stlv tunnel install
    stlv tunnel inspect
    ```

    Run these as your ordinary user. Installation asks for macOS administrator
    authorization. Do not run `stlv dev` with `sudo`.

!!! info "Platform validation"
    Traforo v2 native routing, private DNS, crash cleanup, helper restart, upgrades,
    and legacy migration have passed on macOS 15.7.5 arm64 with Go 1.27.2.
    Both architectures have build checks and x86_64 Go tests pass through Rosetta.
    Native Intel networking and other macOS releases remain unverified. See the
    contributor validation record for the current live AWS acceptance status.

### Start and use the session

For a linked DocumentDB, the infrastructure definition can stay the same:

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import NatConfig, Vpc

vpc = Vpc("main", nat=NatConfig(type="managed", single=True))
db = DocumentDb("todos", vpc=vpc)
Function(
    "api",
    handler="functions/todos.handler",
    requirements=["pymongo"],
    url="public",
    vpc=vpc,
    links=[db],
)
```

Put this inside your app's `@app.run` function. The handler must exist and use
the [DocumentDB link properties](../components/aws/document-db.md#linking).
NAT in this example lets the production Lambda call Secrets Manager; it is
separate from the local dev tunnel.

```bash
stlv dev
# Or: stlv dev staging
```

Stelvio deploys the stub Lambda, discovers the VPCs used by local handlers,
and connects handler dependencies whose VPC explicitly enables a bastion.
Set `bastion=True` (or a `BastionConfig`) and deploy to create the persistent
bastion. Omitting `bastion` defaults to `False`: Stelvio warns and runs the
handler without establishing or checking an external network path. Explicit
`None` is rejected. See [VPC access policy](../components/aws/vpc.md#dev-access-policy).

Wait for the networking readiness output, then call the printed Function URL
or API URL in another terminal. Initial access can take several minutes while
EC2 starts and Systems Manager becomes available. A handler depending on an
unready VPC receives a network-unavailable response before its code runs.
Handlers for healthy VPCs and handlers without a VPC can continue.

### What changes on your machine

The root-owned Traforo helper creates a virtual `utun` interface, scoped routes
for the selected VPC CIDRs, and scoped macOS resolver files. A separate Traforo
forwarder runs as your ordinary user and carries TCP traffic through OpenSSH's
SOCKS proxy. SSH travels over a Systems Manager WebSocket session to the VPC
access instance; no inbound SSH port is opened. A local Python DNS relay sends
the selected private queries through that connection to the VPC DNS resolver.
Unrelated DNS keeps using your normal resolvers.

The Lambda invocation and response use a separate AppSync Events connection.
The stub Lambda does not need VPC attachment in dev mode; your real Lambda
keeps its VPC attachment on an ordinary deploy.

!!! warning "Machine-wide routes"
    Routes apply to all processes on your Mac, not only the local handler.
    Only one managed dev network session can own the helper at a time.
    Overlapping VPC CIDRs, conflicting VPN routes, and foreign resolver files
    are refused rather than overwritten. Multiple VPCs need non-overlapping
    CIDRs and unambiguous DNS ownership.

### Connect without dev mode

```bash
stlv deploy staging
stlv tunnel start staging
```

`tunnel start` reads the deployed environment and connects the VPC dependencies
of its handlers, including linked databases. It does not deploy infrastructure,
start the Lambda bridge, or connect unreferenced VPCs. Use another terminal for
your database client or agent, with the original hostname, port, and TLS settings.
The tunnel stays in the foreground until Ctrl+C. With no eligible dependencies,
it prints warnings where applicable and exits without activating the helper.

### Transport loss and stopping

Stelvio reconnects an interrupted transport using the existing deployed bastion.
It never creates a replacement instance. New dependent dev invocations are
rejected while their VPC is unavailable; unrelated handlers continue. Existing
database calls can fail and are not replayed automatically.

Ctrl+C stops the session and removes its owned routes, interfaces, and resolver
files. If the command dies, the helper detects lease loss or owner death and
cleans those resources itself. A helper restart resumes cleanup from its durable
journal. Persistent bastions, DocumentDB, NAT, and the application remain deployed
and continue to incur their applicable charges.

### Recover an interrupted session

If automatic host cleanup cannot finish:

```bash
stlv tunnel inspect
stlv tunnel cleanup
```

`cleanup` retries removal of stale Stelvio-owned host state and leaves the helper
installed. It refuses live sessions and foreign resources. Preserve the journal
when ownership is uncertain; resolve the reported conflict and retry. This
command does not delete AWS resources. The former temporary AWS access stack and
`tunnel recover` command have been removed.

### Upgrades and uninstall

`stlv tunnel install`, `stlv dev`, and `stlv tunnel start` ensure the packaged
Traforo version is installed. Close active tunnel/dev sessions before replacing
an older helper. Stelvio requests macOS administrator authorization for the
replacement, handles compatible legacy ownership records, and retains evidence
if an interrupted upgrade cannot finish safely. You do not need the old Python
package. Conflicting or unsupported ownership records must be resolved before
replacement; they are never overwritten blindly.

```bash
stlv tunnel uninstall
```

`uninstall` removes the local helper after safe host cleanup and refuses active
sessions. The private AWS plugin cache remains available. Neither cleanup nor
uninstall destroys your application or bastion. Remove the bastion with a deploy
using `bastion=False`, or destroy the application.

Applications deployed with the former temporary policy must set `bastion=True`
and redeploy before managed access works. Existing compatible persistent and
disabled deployment metadata can still be read. Any old temporary AWS resources
must be disposed of through their original ownership workflow before upgrading;
v2 does not adopt or delete that infrastructure.
