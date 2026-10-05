# VPC tunnel app

A minimal Stelvio app: one VPC, one DocumentDB cluster, and one Lambda with a public Function URL. The function is linked to the cluster and attached to the VPC, so a deployed invoke opens a normal TLS connection from inside the VPC. `stlv dev` runs that same handler on your machine. `bastion=True` is the opt-in that gives the tunnel a bastion and lets DocumentDB admit the dev security group. Managed tunnel networking is not accepted yet. See [Known gap](#known-gap).

DocumentDB, the NAT gateway, and the bastion instance are billed for as long as the stack exists. Nothing in this directory deploys them. `uv run stlv deploy` and `uv run stlv dev` both create real AWS resources.

## Layout

`stlv_app.py` builds:

```python
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

`bastion=True` is the dev opt-in on `Vpc`. With it, `DocumentDb` admits `vpc.resources.dev_security_group` on the cluster's resolved port (default 27017). The function uses the VPC's private subnets and the shared app security group, which is the group the cluster already allows. One shared NAT gateway is there so that function can call Secrets Manager for the AWS-managed master password. The cluster itself stays in isolated subnets. The password is the component's default AWS-managed secret. No password is stored in this directory.

`pymongo` is a project dependency so `stlv dev` can import it in this environment, and `requirements=["pymongo"]` so the deployed Lambda package includes it. `boto3` comes from the Lambda runtime and from the local Stelvio install.

## Handler

`functions/ping.handler` is the handler both commands execute. It reads the link through `stlv_resources.Resources.docdb`, which Stelvio generates from these environment variables:

| Property | Environment variable |
| --- | --- |
| `connection_uri` | `STLV_DOCDB_CONNECTION_URI` |
| `ca_file` | `STLV_DOCDB_CA_FILE` |
| `username` | `STLV_DOCDB_USERNAME` |
| `secret_arn` | `STLV_DOCDB_SECRET_ARN` |
| `host` | `STLV_DOCDB_HOST` |

The URI already sets `tls=true`, `tlsCAFile`, `replicaSet=rs0`, and `retryWrites=false`. It has no username or password. The handler also passes `tls=True` and `tlsCAFile=Resources.docdb.ca_file`, then loads the password with `secretsmanager:GetSecretValue` on `secret_arn`. It pings, writes one document to `stelvio.tunnel_proof`, reads it back, and returns JSON with `ok` and `host_matches`. `host_matches` is true when the topology saw `Resources.docdb.host`, or an instance hostname for that same cluster. The response includes that host and the hosts the client saw. It does not include the URI or the password.

On deploy, `ca_file` is the packaged path `stlv_docdb_ca.pem`. Under `stlv dev`, the same property is the absolute cache path `.stelvio/aws/documentdb/global-bundle.pem`. Linking downloads that bundle from Amazon's RDS trust store. The handler never sets `tlsAllowInvalidCertificates` or turns off hostname verification.

## `uv run stlv deploy`

Deploys this app to your personal environment (pass an environment name to use another one). The Lambda runs in AWS, inside the VPC, and the public Function URL invokes `functions/ping.handler` there. After deploy, the CLI prints the Function URL. A `GET` or `POST` to that URL is the in-VPC proof.

```bash
uv run stlv deploy
```

To remove the stack, including the cluster, NAT gateway, and bastion:

```bash
uv run stlv destroy
```

Stopping a later `stlv dev` does not remove the bastion. Deploy again with `bastion` left off, or destroy the app.

## `uv run stlv dev`

`stlv dev` deploys the same app in dev mode: the Lambda in AWS is a stub, and this machine runs `functions/ping.handler`. The flag the CLI accepts is `--network`, with `auto` (the default), `managed`, or `external`.

```bash
uv run stlv dev --network managed
```

`managed` is the mode that is supposed to keep the local handler up only when the dev tunnel is ready. This app gives that mode one used VPC and a bastion, which is what the CLI requires. `uv run stlv dev` with no flag is `auto`. With this app, auto selects managed and then takes the same fail-closed path.

```bash
uv run stlv dev --network external
```

`external` starts the local server and leaves host routes and DNS unchanged. It does not open the managed tunnel, and it does not check that DocumentDB answers.

## Known gap

Managed host networking is not accepted. From `stelvio/dev/handover.md`:

> Managed host networking is not accepted. No OS family has a passing live packet-filter and split-DNS run. External mode leaves host routes and DNS unchanged. `bastion=True` creates billable EC2 infrastructure and does not prove the workstation can reach private addresses. Stopping `stlv dev` leaves that bastion in place.
>
> `stlv dev` (auto) and `stlv dev --network managed`, when one used VPC has bastion metadata, deploy and then fail closed. The code is `network_not_ready` and the detail is `packet filter was not applied`. The deployment stays. The shipped connector does not open SSH. The default helper applicator does not call `pfctl`. sshuttle is not a forwarder.

DocumentDB over that tunnel was not run:

> A02 DocumentDB | BLOCKED | no | No cluster. TLS verification, member discovery, and a nondefault port were not run.

Private DNS was not shown to answer. The handover records that an associated private hosted zone stayed NXDOMAIN. `DocumentDb` publishes the suffix `{region}.docdb.amazonaws.com` for the tunnel allowlist and says that declaring it does not claim a private hosted zone resolves through the bastion.

The shipped session matches that wording. `default_runtime` is "a connector that does not open SSH or install pf." `_ClosedConnector.connect` raises `network_not_ready` with `managed SSH transport is not open`. `UnappliedApplicator` "does not call `pfctl` or `ip`" and returns false, so the packet filter stays unapplied.

`docs/docs/concepts/dev-mode.md` says the same thing: the shipped session opens neither SSH nor a packet filter, so startup fails with `network_not_ready` after the deploy, and the deployment stays. Wiring `bastion=True`, attaching the function to that VPC, and admitting the dev security group on the cluster port is what this app does. It is not a live tunnel acceptance test.
