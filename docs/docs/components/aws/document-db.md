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
    **~$47/month** ($0.065/hour × ~730 hours in `us-east-1`) before storage and
    I/O. A quiet default cluster is typically **~$50/month** including light
    storage, I/O, and Secrets Manager storage (~$0.40). See [Cost](#cost).

## Creating a DocumentDB cluster

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.vpc import Vpc

vpc = Vpc("main")
db = DocumentDb("todos", vpc=vpc)
```

That's a one-instance cluster on engine `8.0`. You need a `Vpc` with at least
two availability zones. The default `Vpc` already has that. `Vpc(..., az=1)`
raises `ValueError` when you construct `DocumentDb`.

The `name` is the DocumentDb component name, not a MongoDB database name. Stelvio
does not create databases or collections; they appear when you first write.
The name must start with a lowercase letter and contain only lowercase letters,
digits, and single hyphens, with no trailing hyphen. Invalid names raise
`ValueError` when you construct `DocumentDb`. Generated AWS identifiers fit
AWS's 63-character limit, including the provider suffix. See
[Customization](#customization) for how Stelvio builds the identifier prefix
and how to override it.

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

vpc = Vpc("main")

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
| `instance_class` | `None` | Instance size, with or without the `db.` prefix (`"t4g.medium"` or `"db.t4g.medium"`). `None` uses a global `instance` customize default if set, otherwise `t4g.medium`. |
| `engine` | `"8.0"` | Engine version: `"8.0"` (default) or `"5.0"`. |
| `deletion_protection` | `False` | Block cluster deletion until you flip this off and redeploy. |
| `backup_retention_period` | `7` | Automated backup retention in days (1–35). |
| `secret_rotation` | `7` | Rotate the AWS-managed master password after this many days (1–1000), or set to `False` to disable automatic rotation until you set a day count again. |

`instance_class` is the size; `instances` is how many. Pass `"t4g.medium"` or
AWS's `"db.t4g.medium"`. Stelvio strips `db.` if present and prepends it when
creating instances.

## Replicas

`instances=1` is a single writer. `instances=2` (up to 16) adds replicas in the
same cluster, not a second cluster. Failover to a replica needs more than one
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
    You cannot downgrade by changing `engine` back. AWS will not major-upgrade a
    `t4g.medium` writer. Resize first, in a separate deploy.

## Networking

The cluster lives in the Vpc's isolated subnets. Creating `DocumentDb` opens
TCP 27017 from that Vpc's [app security group](vpc.md#security-groups).
Functions that join the same Vpc with the default app group (`Function(vpc=vpc)`)
can reach the cluster. See
[Lambda Functions in VPC](vpc.md#lambda-functions-in-vpc).

A Function that links a `DocumentDb` must set `vpc=` to the **same** `Vpc` as
the cluster. Missing `vpc=` or a different Vpc raises `ValueError` when the
Function is created. Linking injects connection properties and IAM; it is not
networking.

The `connection_uri` property does not call Secrets Manager at runtime, so
isolated-subnet Functions can talk to the cluster without NAT.
`connection_uri` stays valid when the password rotates. If you fetch
`secret_arn` at runtime, private subnets need NAT or a Secrets Manager VPC
endpoint. Stelvio does not create the endpoint. `nat="managed"` only routes
private subnets through NAT; isolated subnets stay isolated.

Custom `security_groups` on `VpcAttachment` are kept. When the Function also
links a `DocumentDb`, Stelvio appends the app security group so the cluster's
default ingress still admits it. You can add rules for the other groups
yourself. At most five security groups can be attached (AWS limit), including
the app group when it is appended.

!!! info "Dev mode cannot reach the cluster yet"
    `stlv dev` runs your handlers on your machine, outside the VPC, so the
    cluster is not available in dev mode yet. Access is coming soon.

## Linking

Put the Function in the same Vpc and link it. Linking packages Amazon's
[global RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
into the Function as `stlv_docdb_ca.pem` and injects that path as `ca_file`.
`HttpApi` routes take the same `vpc=` and `links=` options.

!!! warning "The Function must use the cluster's Vpc"
    Missing `vpc=` or a different Vpc raises `ValueError`. Linking injects
    connection properties and IAM; it is not networking.

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

`nat="managed"` is for Secrets Manager HTTPS from private subnets. The cluster
stays in isolated subnets, which still have no NAT. If the Function is in
isolated subnets, add a Secrets Manager interface VPC endpoint. Stelvio does
not create the endpoint.

Linking injects connection properties and grants `secretsmanager:GetSecretValue`
on the AWS-managed master-user secret. Fetch the password at runtime from
`secret_arn` and pass it to `MongoClient` with `connection_uri`.

### Password rotation

DocumentDB's AWS-managed master password rotates every seven days by default.
Set `secret_rotation` to another number of days to change that schedule. Set
`secret_rotation=False` to disable automatic rotation. Keeping a long-lived
database password does not follow security best practices.

!!! warning "Keep the AWS-managed password"
    Leave `manage_master_user_password` enabled (the default). Disabling it
    through customize means the default link cannot resolve a secret ARN.

### Link Properties

For a cluster named `todos`, the linked function receives these properties:

| `stlv_resources` property | Description |
|---------------------------|-------------|
| `Resources.todos.host` | Cluster writer endpoint |
| `Resources.todos.reader_host` | Cluster reader endpoint |
| `Resources.todos.port` | Port (default `27017`) |
| `Resources.todos.username` | Master username (default `stelvio`) |
| `Resources.todos.secret_arn` | Secrets Manager ARN for the AWS-managed password |
| `Resources.todos.replica_set` | Replica set name (`rs0`) |
| `Resources.todos.ca_file` | Path to Amazon's CA bundle in the Lambda package |
| `Resources.todos.connection_uri` | Writer `mongodb://` URI without username or password (`tls`, CA file, replica set, `retryWrites=false`). Safe to use with rotation. |

### Link Permissions

Linked Lambda functions receive:

- `secretsmanager:GetSecretValue` on the AWS-managed master-user secret

DocumentDB authenticates with username and password. There are no DocumentDB
data-plane IAM actions.

### Using the cluster from Lambda

Fetch the password from Secrets Manager in the handler so a later invocation
sees a rotated password. MongoDB database and collection names are yours to
choose. They are not the component `name`:

```python
import json

import boto3
from pymongo import MongoClient
from stlv_resources import Resources

secrets = boto3.client("secretsmanager")


def handler(event, context):
    secret = json.loads(
        secrets.get_secret_value(SecretId=Resources.todos.secret_arn)["SecretString"]
    )
    client = MongoClient(
        Resources.todos.connection_uri,
        username=Resources.todos.username,
        password=secret["password"],
    )
    collection = client.app.items
    collection.replace_one({"_id": "hello"}, {"_id": "hello", "ok": True}, upsert=True)
    return {"item": collection.find_one({"_id": "hello"})}
```

`host`, `port`, `ca_file`, and `replica_set` are still injected if you need the
pieces.

!!! info "DocumentDB is not full MongoDB"
    TLS is required. The URI already sets `replicaSet=rs0` and `retryWrites=false`.
    DocumentDB does not support retryable writes, and clients that omit the
    replica set name often fail to discover the cluster. APIs and defaults that
    assume MongoDB Atlas or a self-hosted replica set may not apply.

## Cost

Stelvio uses Standard storage (pay-per-use I/O). Prices below are on-demand in
`us-east-1`; check [AWS DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/)
and the [AWS Pricing Calculator](https://calculator.aws/#/createCalculator/DocumentDB)
for your region.

A default cluster bills:

- Instance: one `t4g.medium` at **$0.065/hour**, about **~$47/month**
- Storage: **$0.10/GB-month**
- I/O: **$0.20 per million requests**
- Secrets Manager storage for the managed password: about **~$0.40/month**

NAT (**~$37 to ~$73/month** with `nat="managed"`) is needed for the Function to
call `GetSecretValue` from private subnets. Isolated subnets still have no NAT.
See [NAT](vpc.md#nat).

## Customization

The `DocumentDb` component supports the `customize` parameter to override
underlying Pulumi resource properties. For an overview of how customization
works, see the [Customization guide](../../concepts/customization.md).

By default Stelvio passes an AWS identifier prefix of `{app}-{env}-{name}-`,
and DocumentDB appends a unique suffix. If the app or environment prefix does
not start with a letter, Stelvio adds `stlv-`. Cluster instance prefixes also
include the instance number. You can override `cluster_identifier`,
`cluster_identifier_prefix`, `identifier`, or `identifier_prefix` through
customize. The identifier and prefix forms are mutually exclusive for each
AWS resource.

### Resource Keys

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `cluster` | [ClusterArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/cluster/#inputs) | The DocumentDB cluster |
| `instance` | [ClusterInstanceArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterinstance/#inputs) | Cluster instances (all of them) |
| `subnet_group` | [SubnetGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/subnetgroup/#inputs) | Subnet group (isolated subnets) |
| `parameter_group` | [ClusterParameterGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterparametergroup/#inputs) | Cluster parameter group |
| `security_group` | [SecurityGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/securitygroup/#inputs) | Cluster security group |
| `secret_rotation` | [SecretRotationArgs](https://www.pulumi.com/registry/packages/aws/api-docs/secretsmanager/secretrotation/#inputs) | AWS-managed master-secret rotation schedule |

### Example

To keep a cluster whose data must survive accidental deletion:

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.vpc import Vpc

vpc = Vpc("main")
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
Stelvio cannot restore snapshots. Do that in the AWS console or CLI.

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
    `db.resources.security_group` instead. That grants network access only.
    The client still needs credentials.

!!! warning "Replacing `vpc_security_group_ids` drops the generated group"
    Stelvio always creates a cluster security group and attaches app-SG
    ingress to that generated group. A dict override of
    `cluster.vpc_security_group_ids` replaces the list, so the generated
    group is no longer on the cluster and linked functions cannot connect
    unless you add equivalent ingress on the groups you supply. Stelvio does
    not add ingress to caller-supplied security groups.

    To keep generated ingress and add extra groups, append with a callable:

    ```python
    def append_security_group(props):
        return props | {
            "vpc_security_group_ids": [*props["vpc_security_group_ids"], extra_sg.id],
        }

    db = DocumentDb("todos", vpc=vpc, customize={"cluster": append_security_group})
    ```

    `extra_sg` is a security group you already created. Replacing the list
    entirely is valid when you own ingress on the groups you supply.

## Next Steps

- [Working with VPC](vpc.md): Isolated subnets, NAT, and Lambda in a VPC
- [Working with Lambda Functions](lambda.md): Functions that connect to the cluster
- [Working with HTTP APIs](http-api.md): Routes that pass `vpc=` and `links=[db]`
- [Linking](../../concepts/linking.md): How Stelvio injects connection properties and IAM
- [Customization](../../concepts/customization.md): Override Pulumi resource properties
- [Tags](../../concepts/tags.md): Tag your resources
