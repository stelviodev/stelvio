# Working with DocumentDB in Stelvio

This guide explains how to create [Amazon DocumentDB](https://docs.aws.amazon.com/documentdb/)
clusters with Stelvio. You'll learn how to size a cluster, put it in a VPC, link
it to Lambda, and connect with the MongoDB drivers.

DocumentDB is a MongoDB-compatible database that runs inside a VPC. Use it when
you need Mongo-compatible queries and always-on capacity. For key-value data
billed per request, start with [`DynamoTable`](dynamo-db.md).

By default, `DocumentDb` creates a private, TLS-required, encrypted cluster on
engine `8.0` with an AWS-managed master password. The cluster sits in the
Vpc's isolated subnets and accepts traffic from that Vpc's shared app security
group.

!!! warning "DocumentDB costs money"
    Instances bill while they run, not per request. One `t4g.medium` is
    **~$47/month** before storage and I/O. Storage is **$0.10/GB-month**; I/O is
    **$0.20 per million requests**. Functions that fetch the password also need
    NAT (**~$37 to ~$73/month**) or a Secrets Manager VPC endpoint. See
    [Cost](#cost).

## Creating a DocumentDB cluster

Creating a DocumentDB cluster in Stelvio is straightforward:

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.vpc import Vpc

vpc = Vpc("main")
db = DocumentDb("todos", vpc=vpc)
```

That's a one-instance cluster on engine `8.0`. You need a `Vpc` with at least
two availability zones — the default `Vpc` already has that. `Vpc(..., az=1)`
raises `ValueError` when you construct `DocumentDb`.

The `name` is the AWS cluster identifier, not a MongoDB database name. Stelvio
does not create databases or collections; they appear when you first write.
The name must start with a lowercase letter and contain only lowercase letters,
digits, and single hyphens, with no trailing hyphen. Invalid names raise
`ValueError` when you construct `DocumentDb`. Stelvio prefixes the AWS
identifier with `{app}-{env}-`.

!!! warning "The first deploy takes 10 to 20 minutes"
    AWS provisions the cluster and every instance before the deploy finishes, and
    destroying it is slow too. A stuck-looking deploy is usually just AWS.

!!! warning "Destroying the cluster deletes your data"
    `stlv destroy` drops the cluster with no final snapshot. For anything you
    care about, set `deletion_protection=True` and keep a snapshot through
    [Customization](#customization).

Elastic clusters, serverless, global clusters, snapshot restore, and `stlv dev`
access to the cluster are not supported.

### Configuration

```python
from stelvio.aws.document_db import DocumentDb, DocumentDbConfig
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")

# Keyword arguments
db = DocumentDb("todos", vpc=vpc, instances=2)

# Or using DocumentDbConfig
db = DocumentDb("todos", config=DocumentDbConfig(vpc=vpc, instances=2))
```

You can also pass a dict via `config=`. Don't mix kwargs and `config=` in the
same call.

Available configuration options:

| Option | Default | Description |
|--------|---------|-------------|
| `vpc` | (required) | Existing `Vpc`. The cluster uses its isolated subnets. |
| `instances` | `1` | Number of cluster instances (1 to 16). Extra instances are replicas. |
| `instance_class` | `None` | Instance size without the `db.` prefix (for example `"t4g.medium"`). `None` uses `t4g.medium`. |
| `engine` | `"8.0"` | Engine version: `"8.0"` (default) or `"5.0"`. |
| `deletion_protection` | `False` | Block cluster deletion until you flip this off and redeploy. |
| `backup_retention_period` | `7` | Automated backup retention in days (1–35). |

`instance_class` is the size; `instances` is how many. Pass `t4g.medium`, not
AWS's `db.t4g.medium` form — Stelvio prepends `db.`.

## Replicas

`instances=1` is a single writer. `instances=2` (up to 16) adds replicas in the
same cluster — not a second cluster. Failover to a replica needs more than one
instance.

The writer endpoint is `host`. Read-only traffic can use `reader_host`, which
load-balances across replicas. With one instance, `reader_host` still exists
but points at that same instance.

## Engine version

Default is `"8.0"`. Pass `engine="5.0"` to opt in to 5.0.

!!! warning "Changing engine upgrades the live cluster"
    Changing `engine="5.0"` to `engine="8.0"` is an in-place major version
    upgrade with downtime. Stelvio blocks that unless you opt in with
    `customize={"cluster": {"allow_major_version_upgrade": True}}`. You must
    still meet the
    [AWS upgrade prerequisites](https://docs.aws.amazon.com/documentdb/latest/devguide/docdb-mvu.html).
    You cannot downgrade by changing `engine` back.

AWS will not upgrade a `t4g.medium` writer. Resize to `r6g.large` (or
`r5.large`) and let that deploy finish before you change `engine`. Changing
both in one deployment does not guarantee the resize happens first. Without
`customize={"cluster": {"apply_immediately": True}}`, AWS may wait for the
maintenance window.

## Networking

The cluster lives in the Vpc's isolated subnets. Creating `DocumentDb` opens
TCP 27017 from that Vpc's [app security group](vpc.md#security-groups).
Functions that join the same Vpc with the default app group —
`Function(vpc=vpc)` — can reach the cluster. See
[Lambda Functions in VPC](vpc.md#lambda-functions-in-vpc).

A Function that links a `DocumentDb` must set `vpc=` to the **same** `Vpc` as
the cluster. Missing `vpc=` or a different Vpc raises `ValueError` when the
Function is created. Linking injects env vars and IAM; it is not networking.

Private subnets need NAT or a Secrets Manager VPC endpoint so the Function can
fetch the password. Isolated-subnet Functions can open TCP to the cluster, but
still need that endpoint for Secrets Manager. Stelvio does not create the
endpoint. `nat="managed"` only routes private subnets through NAT; isolated
subnets stay isolated.

Custom `security_groups` on `VpcAttachment` replace the app group. Those
functions are not admitted by DocumentDB's ingress; you wire their rules
yourself.

`stlv dev` cannot reach the cluster.

## Linking

Put the Function in the same Vpc and link it. Stelvio packages Amazon's
DocumentDB CA bundle into the Function and injects its path as `ca_file`.
`HttpApi` routes take the same `vpc=` and `links=` options.

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")
db = DocumentDb("todos", vpc=vpc)
Function(
    "api",
    handler="functions/todos.handler",
    requirements=["pymongo"],
    vpc=vpc,
    links=[db],
)
```

Linking injects connection properties and grants
`secretsmanager:GetSecretValue` on the AWS-managed master-user secret. The
password stays in Secrets Manager; it is never an environment variable.

!!! warning "Keep the AWS-managed password"
    Leave `manage_master_user_password` enabled (the default). Disabling it
    through customize means the default link cannot resolve a secret ARN.

For a cluster named `todos`, the linked function receives these properties:

| `stlv_resources` property | Environment variable | Description |
|---------------------------|----------------------|-------------|
| `Resources.todos.host` | `STLV_TODOS_HOST` | Cluster writer endpoint |
| `Resources.todos.reader_host` | `STLV_TODOS_READER_HOST` | Cluster reader endpoint |
| `Resources.todos.port` | `STLV_TODOS_PORT` | Port (default `27017`) |
| `Resources.todos.username` | `STLV_TODOS_USERNAME` | Master username (default `stelvio`) |
| `Resources.todos.secret_arn` | `STLV_TODOS_SECRET_ARN` | Secrets Manager ARN for the AWS-managed password |
| `Resources.todos.replica_set` | `STLV_TODOS_REPLICA_SET` | Replica set name (`rs0`) |
| `Resources.todos.ca_file` | `STLV_TODOS_CA_FILE` | Path to Amazon's CA bundle in the Lambda package |

### Link Permissions

Linked Lambda functions receive:

- `secretsmanager:GetSecretValue` on the AWS-managed master-user secret

DocumentDB authenticates with username and password. There are no DocumentDB
data-plane IAM actions.

### Using the cluster from Lambda

Fetch the password and connect with TLS. `replica_set` and `ca_file` come from
the link. MongoDB database and collection names are yours to choose — they are
not the component `name`:

```python
import json

import boto3
from pymongo import MongoClient
from stlv_resources import Resources

def handler(event, context):
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=Resources.todos.secret_arn,
    )
    password = json.loads(secret["SecretString"])["password"]

    client = MongoClient(
        host=Resources.todos.host,
        port=int(Resources.todos.port),
        username=Resources.todos.username,
        password=password,
        tls=True,
        tlsCAFile=Resources.todos.ca_file,
        replicaSet=Resources.todos.replica_set,
        retryWrites=False,
    )
    try:
        collection = client.app.items
        collection.replace_one({"_id": "hello"}, {"_id": "hello", "ok": True}, upsert=True)
        return {"item": collection.find_one({"_id": "hello"})}
    finally:
        client.close()
```

!!! info "DocumentDB is not full MongoDB"
    TLS is required. Use the injected `replica_set` (`rs0`) and
    `retryWrites=False` — DocumentDB does not support retryable writes, and
    clients that omit the replica set name often fail to discover the cluster.
    APIs and defaults that assume MongoDB Atlas or a self-hosted replica set
    may not apply.

DocumentDB rotates its managed password every seven days by default. See
[AWS-managed password rotation](https://docs.aws.amazon.com/documentdb/latest/devguide/docdb-secrets-manager.html).
The example fetches current credentials and closes its client on each
invocation. If you reuse connections across invocations, handle authentication
failures by fetching the current secret, closing the stale client, and
reconnecting once. Never log the secret or a connection string containing the
password.

## Cost

DocumentDB bills four things, independently. Stelvio uses Standard storage
(pay-per-use I/O). Prices below are on-demand in `us-east-1`; check
[AWS DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/) for your
region.

### Instances

Each cluster instance is always on. The default `t4g.medium` is **$0.065/hour**,
about **~$47/month**. `instances=2` is two of those (~$94/month). Instances
bill per second with a 10-minute minimum.

`t4g.medium` runs in unlimited burst mode. If average CPU over 24 hours exceeds
the baseline, extra CPU credits are **$0.09 per vCPU-hour**.

### Storage

Storage is **$0.10 per GB-month**. That covers documents, indexes, and change
stream data. AWS replicates the volume across three AZs; you pay for one
logical copy. Storage grows automatically — you do not provision a disk size.

A nearly empty cluster is a few dollars. 50 GB is **$5/month**; 500 GB is
**$50/month**.

### I/O

I/O is **$0.20 per million requests**, billed separately from storage. Reads
and writes against the cluster volume count: `find`, `insert`, `update`,
`delete`, change streams, TTL indexes, and tools like `mongodump`.

A page already in instance memory is not billed again. A working set that fits
in RAM keeps I/O down; a too-small instance, unused indexes, or a write-heavy
workload can make I/O the largest line item.

10 million I/Os is **$2/month**. 200 million is **$40/month**. Watch
`VolumeReadIOPs` and `VolumeWriteIOPs` in CloudWatch.

### Backup

Automated backup storage up to 100% of your data storage is free. Extra backup
storage, and manual snapshots you keep after the retention period, is about
**$0.02/GB-month**. `stlv destroy` skips a final snapshot by default; snapshots
you already took stay in the account and keep billing until you delete them.

### What a typical Stelvio cluster costs

A default one-instance cluster, mostly idle, with a few GB of data:

| Line | Monthly |
|------|---------|
| 1 × `t4g.medium` | ~$47 |
| Storage (10 GB) | $1 |
| I/O (light) | a few dollars |
| **DocumentDB total** | **~$50** |

The same cluster with 50 GB of data and 100 million I/Os in a month is about
**$47 + $5 + $20 = ~$72** before NAT.

Functions that fetch the password from Secrets Manager need NAT or a Secrets
Manager interface VPC endpoint. With `nat="managed"`, NAT is **~$73/month**
(one gateway per AZ) or **~$37/month** with
`nat=NatConfig(type="managed", single=True)`, plus data charges. See
[NAT](vpc.md#nat). An interface endpoint has its own hourly and data charges;
see [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/).

## Customization

The `DocumentDb` component supports the `customize` parameter to override
underlying Pulumi resource properties. For an overview of how customization
works, see the [Customization guide](../../concepts/customization.md).

### Resource Keys

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `cluster` | [ClusterArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/cluster/#inputs) | The DocumentDB cluster |
| `instance` | [ClusterInstanceArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterinstance/#inputs) | Cluster instances (all of them) |
| `subnet_group` | [SubnetGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/subnetgroup/#inputs) | Subnet group (isolated subnets) |
| `parameter_group` | [ClusterParameterGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterparametergroup/#inputs) | Cluster parameter group |
| `security_group` | [SecurityGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/securitygroup/#inputs) | Cluster security group |

### Example

To keep a cluster whose data must survive accidental deletion:

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")
db = DocumentDb(
    "todos",
    vpc=vpc,
    deletion_protection=True,
    customize={
        "cluster": {
            "skip_final_snapshot": False,
            "final_snapshot_identifier": "myapp-prod-todos-final-20260918",
        },
    },
)
```

Choose a snapshot identifier that is not already in use in your account and
region. To destroy this cluster, first set `deletion_protection=False` and
deploy, keeping the snapshot settings. Then destroy it. The final snapshot
stays available for restoration and incurs storage charges until deleted.
Stelvio cannot restore snapshots — do that in the AWS console or CLI.

!!! warning "`instance` applies to every instance"
    There is one customize key for all cluster instances. A dict value is
    applied to every instance identically. Use a callable if you need
    per-instance values.

!!! warning "Customizing `parameters` replaces the whole list"
    `parameter_group.parameters` replaces Stelvio's list rather than appending.
    Stelvio puts `tls=enabled` back unless your list already has a `tls`
    parameter. Include `tls` yourself if you want a different value.

!!! warning "Do not set inline security-group rules through customize"
    DocumentDB already manages a standalone ingress rule on the cluster
    security group. Mixing inline `ingress` / `egress` with standalone rules
    can overwrite rules or produce perpetual deployment differences. Add a
    separate `SecurityGroupIngressRule` that references
    `db.resources.security_group` instead. That grants network access only —
    the client still needs credentials.

## Next Steps

- [Working with VPC](vpc.md) — Isolated subnets, NAT, and Lambda in a VPC
- [Working with Lambda Functions](lambda.md) — Functions that connect to the cluster
- [Working with HTTP APIs](http-api.md) — Routes that pass `vpc=` and `links=[db]`
- [Linking](../../concepts/linking.md) — How Stelvio injects env vars and IAM
- [Customization](../../concepts/customization.md) — Override Pulumi resource properties
- [Tags](../../concepts/tags.md) — Tag your resources
