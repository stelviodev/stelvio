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
2. Install the [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
   and make `session-manager-plugin` available on your `PATH`. Stelvio uses the
   macOS OpenSSH tools and your configured AWS credentials. Those credentials
   need permission to deploy the access resources, use Systems Manager and
   EC2 Instance Connect, and read any secrets your local handler uses.
   Managed access supports AWS profiles and the normal SDK credential chain.
   Explicit credentials, assume-role settings, custom endpoints, and proxies
   configured directly on a custom Pulumi provider are rejected. Configure the
   supported AWS credential source before starting the session.
3. Install your handler's dependencies in the Python environment running `stlv`.
   For DocumentDB, install `pymongo` there as well as declaring it in the
   Function's `requirements`. Lambda packaging does not install local packages.
4. Install the helper once from that Stelvio environment:

    ```bash
    stlv tunnel install
    stlv tunnel inspect
    ```

    Run these as your ordinary user. Installation asks for macOS administrator
    authorization. Do not run `stlv dev` with `sudo`.

!!! info "Platform validation"
    Native routing, DNS, recovery, and the DocumentDB workflow have passed on
    macOS 15.7.5 arm64. Intel assets have build and Rosetta checks; native Intel
    networking and other macOS releases remain unverified. The current Go 1.27.2
    assets pass build, race, packaging, and command checks; the native and AWS
    acceptance runs used the earlier Go 1.25.3 build.

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
and starts access for each enabled VPC. With `bastion` omitted, access resources
are temporary. For persistent access or custom private DNS domains, see
[VPC policy and DNS](../components/aws/vpc.md#dev-access-policy).

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

### Transport loss and stopping

If the connection drops, Stelvio withdraws readiness for that VPC and tries to
reconnect. New dependent invocations receive an unavailable response until access is
restored. Already-running handlers and database sockets can fail;
Stelvio does not replay their requests. Retry writes according to your
application's own rules. Changed server identity or revoked authorization
requires attention instead of an automatic trust change.

Press `Ctrl+C` and wait for shutdown to finish. Stelvio closes the transports,
removes owned routes and resolvers, and deletes temporary access resources.
Persistent bastions, DocumentDB, NAT, and the application stay in AWS and can
continue billing. Run `stlv deploy` for the same environment to restore your
production Lambda, or `stlv destroy` to remove the application, including its
database. See [cost and lifetime](../components/aws/vpc.md#dev-access-cost-and-lifetime).

### Recover an interrupted session

Keep the terminal output and recovery records if cleanup reports a failure.
Do not delete the helper journal, local recovery files, or the session's remote
state to make an error disappear.

1. Stop the original dev process. Recovery refuses to take over a live owner.
2. Inspect and reconcile stale host resources:

    ```bash
    stlv tunnel inspect
    stlv tunnel reconcile
    stlv tunnel inspect
    ```

3. For AWS cleanup, run the **`AWS recovery:` command printed by Stelvio** as
   the original user with the original credential sources. It includes the
   home state bucket/account/region, target account/region, app, environment,
   session ID, and any selected profiles. See the
   [`tunnel recover` reference](../intro/using-cli.md#tunnel-recover).
   If the process died before printing it, retain the logs and ownership state
   and establish those exact identities before recovery. Do not guess a session.
4. Wait for successful cleanup before starting another managed session.
   Restore expired AWS credentials and rerun the same recovery command when
   necessary. Host reconciliation alone does not delete AWS resources.

### Remove or upgrade the helper

After closing dev sessions and completing recovery:

```bash
stlv tunnel cleanup
```

This removes owned host resources and uninstalls the helper. It does not destroy
the application or its AWS access resources. Active or uncertain installations
cannot be overwritten or silently removed.

For an upgrade that changes the binary, run cleanup using the **matching old
Stelvio installation**, then install with the new one. Keep the old Python
environment available until cleanup succeeds. Legacy helper installations also
require their matching package's cleanup; Traforo does not automatically
replace them. The installed image is shared across Python environments, and
both helper and forwarder use that same verified image.

## Limitations

### Target Architecture

Stelvio's dev mode will execute your local Lambda functions natively, i.e. the Python interpreter is used on your workstation's operating system and CPU architecture, no matter your Lambda function definition. There is no emulator in place that would match the os/architecture on AWS with the one on your workstation.

Since your workstation is likely equipped with more RAM than your Lambda function, this also mean that you're less likely to run into memory limits on your workstation.

### Request/Response Size Limits

Dev mode supports the same payload limits as [AWS Lambda](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) (internally, large payloads are chunked into multiple AppSync events and reassembled on the other end):

- **6 MB** for request and response (synchronous invocations)
- **1 MB** for asynchronous invocations
