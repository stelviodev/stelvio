# VPC tunnel app

One VPC, one DocumentDB cluster, and one Lambda with a public Function URL.
`stlv dev` executes `functions/ping.handler` locally while that public URL stays
in AWS. Traforo makes the private database reachable using its real hostnames
and port, with verified TLS and replica discovery.

## Setup and run

Managed networking targets macOS 15+ on arm64 and x86_64. `stlv tunnel install`
downloads and verifies its pinned private AWS Session Manager plugin. Configure
AWS credentials and a region with permissions to deploy this app, use SSM and
EC2 Instance Connect, and read its managed secret. Stelvio downloads the checksum-pinned Traforo release into its private binary cache;
users do not need Go or Xcode. During private-repository development, set
`GITHUB_TOKEN` explicitly (for example, `export GITHUB_TOKEN="$(gh auth token)"` in
your testing shell). Production uses only an environment-provided token, when set;
public releases can be downloaded without authentication.

```bash
cd spikes/vpc-tunnel-app
uv sync
uv run stlv tunnel install
uv run stlv tunnel inspect
uv run stlv dev
```

Run as your ordinary user. The helper install asks for macOS administrator
authorization. Wait for network readiness and the printed Function URL, then
call it from another terminal:

```bash
curl '<printed Function URL>'
```

The response should contain `ok: true`, `read_back: true`, and
`host_matches: true`, plus `expected_host` and `seen_hosts`. The handler pings,
writes a document to `stelvio.tunnel_proof`, reads it back, and checks the
cluster/member names discovered by PyMongo. It does not expose the password.
An unavailable VPC gates new dependent invocations before the handler runs.
Transport reconnection does not replay requests or recover in-flight sockets.

## Infrastructure and lifetime

Inside the existing `@app.run` function, the app defines:

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import NatConfig, Vpc

vpc = Vpc("net", nat=NatConfig(type="managed", single=True), bastion=True)
docdb = DocumentDb("docdb", vpc=vpc)
Function(
    "api",
    handler="functions/ping.handler",
    requirements=["pymongo"],
    url="public",
    vpc=vpc,
    links=[docdb],
    memory=256,
)
```

`bastion=True` creates a persistent access instance through deployment, which stays
between sessions. Omitted or `bastion=False` disables managed access: dependent
handlers warn and run using your own network path. No public SSH ingress is opened.
SSH travels through SSM to the access instance's loopback SSH server. The
instance has a public IP for outbound AWS connectivity.

The production Lambda uses private subnets and the shared app security group;
DocumentDB stays in isolated subnets. A single managed NAT gateway lets the
production handler call Secrets Manager for the AWS-managed password. Local dev
uses your local AWS credentials for Secrets Manager. PyMongo is both a project
dependency for local execution and a Function requirement for Lambda packaging.

DocumentDB, NAT, the persistent access instance, storage, and public IPv4
addresses are billable while they exist. `stlv dev` deploys real resources.
Stopping it does not destroy the app or persistent access.

## Connection settings

The handler reads `Resources.docdb` from generated `stlv_resources`:
`connection_uri`, `ca_file`, `username`, `secret_arn`, and `host`. The URI contains
`tls=true`, `tlsCAFile`, `replicaSet=rs0`, and `retryWrites=false`, without a
password. The handler retrieves the password from Secrets Manager. Keep these
settings and the original database hostname; do not switch to localhost or
disable certificate or hostname verification.

On deploy the CA file is bundled as `stlv_docdb_ca.pem`. During dev, linking
uses the absolute cache path under `.stelvio/aws/documentdb/global-bundle.pem`.
Traforo routes the VPC's private IPv4 TCP traffic; scoped DNS uses the nonroot Traforo Go relay and the VPC resolver. Lambda invocation/results use a separate AppSync
Events connection. Only one managed session can own the helper on a machine.

## Standalone access, cleanup, and removal

After deployment, connect another local database client without running dev mode:

```bash
uv run stlv tunnel start
```

This uses the deployed handler dependencies and runs until interrupted. It does
not deploy, start the invocation bridge, or create an access instance.

Press Ctrl+C and wait for shutdown. To restore the production Lambda:

```bash
uv run stlv deploy
```

To remove the application, including DocumentDB, NAT, and persistent access:

```bash
uv run stlv destroy
```

Destroy deletes database data. To uninstall the local helper after sessions finish:

```bash
uv run stlv tunnel uninstall
```

The helper cleans host networking after normal shutdown or command death. If
cleanup remains uncertain, retain logs and ownership state, inspect the helper,
and run `stlv tunnel cleanup` after stopping live sessions. Cleanup retains the
installation; `reconcile` is a compatibility alias. Neither command removes AWS
resources. A new Stelvio package can upgrade the helper directly with
`stlv tunnel install`; active sessions must stop first. The private plugin cache
remains after uninstall.

The full user workflow and policy are in
[dev mode](../../docs/docs/concepts/dev-mode.md) and
[VPC access](../../docs/docs/components/aws/vpc.md#dev-access-policy).

## Validation boundary

V2 native routing/DNS, upgrades, crash cleanup, and failed-cleanup recovery passed
on macOS 15.7.5 arm64 with Go 1.27.2. The current default-profile AWS run passed
dev and standalone TLS, member discovery, read/write, and transport reconnection;
application teardown and independent absence audits passed. Both architecture
builds and Rosetta tests passed; native Intel networking and other macOS releases
remain unverified. Linux, WSL, and Windows VPC backends are not implemented.
