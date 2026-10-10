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
    Instances bill while they run, not per request. A default one-instance
    cluster is about **$57/month**, see [Cost](#cost). Linked functions also
    need NAT, which has its own [cost](vpc.md#cost).

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

### Instance class capacity

!!! warning "Capacity is per zone, and it changes"
    AWS does not offer every instance class in every availability zone, and
    which zones have room changes. If creating the instance fails, set a
    different `instance_class` and deploy again, or try later. The zones named
    in the error are only right for that moment. Changing `az` on the Vpc is
    not the fix.

The `name` is the DocumentDb component name, not a MongoDB database name. Stelvio
does not create databases or collections; they appear when you first write.
The name must start with a lowercase letter and contain only lowercase letters,
digits, and single hyphens, with no trailing hyphen. Invalid names raise
`ValueError` when you construct `DocumentDb`. See
[Customization](#customization) to override the AWS identifiers.

!!! warning "The first deploy takes 10 to 20 minutes"
    AWS provisions the cluster and every instance before the deploy finishes, and
    destroying it is slow too. A stuck-looking deploy is usually just AWS.

!!! warning "Destroying the cluster deletes your data"
    `stlv destroy` drops the cluster with no final snapshot. For anything you
    care about, set `deletion_protection=True` and keep a snapshot through
    [Customization](#customization).

Elastic clusters, serverless, global clusters, and snapshot restore are not
supported. `stlv dev` access to the cluster is not supported yet.

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
| `instance_class` | `None` | Instance size, with or without the `db.` prefix (`"t3.medium"` or `"db.t3.medium"`). `None` uses `t3.medium`. AWS does not offer every class in every availability zone. See [Instance class capacity](#instance-class-capacity). |
| `engine` | `"8.0"` | Engine version: `"8.0"` (default) or `"5.0"`. |
| `deletion_protection` | `False` | Block cluster deletion until you flip this off and redeploy. |
| `backup_retention_period` | `7` | Automated backup retention in days (1–35). |
| `secret_rotation` | `7` | Rotate the AWS-managed master password after this many days (1–1000), or set to `False` to disable automatic rotation until you set a day count again. |

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
    upgrade with downtime. AWS rejects that unless you opt in with
    `customize={"cluster": {"allow_major_version_upgrade": True}}`. You must
    still meet the
    [AWS upgrade prerequisites](https://docs.aws.amazon.com/documentdb/latest/devguide/docdb-mvu.html).
    You cannot downgrade by changing `engine` back. AWS says to resize a
    burstable writer to at least `r5.large` or `r6g.large` before a major
    upgrade; it may fail otherwise.

    Resize first, in a separate deploy:

    1. Change `instance_class` and deploy.
    2. Wait until the resize has applied. By default it waits for the
       maintenance window, and an upgrade before then still hits `t3.medium`.
       Set `customize={"instance": {"apply_immediately": True}}` to resize
       right away.
    3. Only then change `engine` with
       `customize={"cluster": {"allow_major_version_upgrade": True}}`.

## Networking

The cluster lives in the Vpc's isolated subnets. Creating `DocumentDb` opens
TCP 27017 from that Vpc's [app security group](vpc.md#security-groups).
Functions that join the same Vpc with the default app group (`Function(vpc=vpc)`)
can reach the cluster. See
[Lambda Functions in VPC](vpc.md#lambda-functions-in-vpc).

With your own `security_groups` on `VpcAttachment`, the cluster still admits
only the app security group. Add an ingress rule on
`db.resources.security_group` yourself:

```python
from pulumi_aws.vpc import SecurityGroupIngressRule

SecurityGroupIngressRule(
    "todos-from-my-sg",
    security_group_id=db.resources.security_group.id,
    referenced_security_group_id=my_sg.id,
    ip_protocol="tcp",
    from_port=27017,
    to_port=27017,
)
```

## Linking

Put the Function in the same Vpc and link it.
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

`connection_uri` has no password, so the Function reads it from Secrets
Manager and needs a route there: `nat="managed"` for private subnets, or a
Secrets Manager interface VPC endpoint for isolated subnets (Stelvio does not
create the endpoint). The cluster itself stays in isolated subnets.

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
| `Resources.todos.ca_file` | Path to Amazon's CA bundle (see below) |
| `Resources.todos.connection_uri` | Writer `mongodb://` URI without username or password (`tls`, CA file, replica set, `retryWrites=false`). Safe to use with rotation. |

!!! info "`ca_file` and the CA bundle"
    On `stlv deploy`, `stlv diff` and `stlv dev`, linking downloads Amazon's
    [global RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
    and caches it at `.stelvio/aws/documentdb/global-bundle.pem` for 24 hours.
    Deploy packages that bundle into each linked Function as `stlv_docdb_ca.pem`,
    and `ca_file` (and the URI's `tlsCAFile`) use that package-relative path.
    In `stlv dev`, the same properties use the absolute cache path so the local
    handler can open the file without staging. This needs network access to
    `truststore.pki.rds.amazonaws.com`. If the download fails and there is no
    cached copy younger than 24 hours, the command fails.

### Link Permissions

Linked Lambda functions receive:

- `secretsmanager:GetSecretValue` on the AWS-managed master-user secret

DocumentDB authenticates with username and password. There are no DocumentDB
data-plane IAM actions.

### Using the cluster from Lambda

Fetch the password and create the `MongoClient` at module level, so warm
invocations reuse both:

```python
import json

import boto3
from pymongo import MongoClient
from pymongo.errors import OperationFailure
from stlv_resources import Resources

AUTHENTICATION_FAILED = 18

secrets = boto3.client("secretsmanager")


def connect() -> MongoClient:
    secret = secrets.get_secret_value(SecretId=Resources.todos.secret_arn)
    return MongoClient(
        Resources.todos.connection_uri,
        username=Resources.todos.username,
        password=json.loads(secret["SecretString"])["password"],
    )


client = connect()


def handler(event, context):
    global client
    try:
        item = client.app.items.find_one({"_id": event["id"]})
    except OperationFailure as error:
        if error.code != AUTHENTICATION_FAILED:
            raise
        refreshed = connect()
        client.close()
        client = refreshed
        item = client.app.items.find_one({"_id": event["id"]})
    return {"item": item}
```

After a [password rotation](#password-rotation), new connections from a warm
function fail with `AuthenticationFailed` (code `18`). The handler then fetches
the current password and opens a new client; only after that succeeds does it
close the stale client and swap it in. Only the read is retried. Don't replay a
write automatically. You can't tell whether it went through. Never log the
secret or a connection string containing the password.

`host`, `port`, `ca_file`, and `replica_set` are still injected if you need the
pieces.

!!! info "DocumentDB is not full MongoDB"
    TLS is required. The URI already sets `replicaSet=rs0` and `retryWrites=false`.
    DocumentDB does not support retryable writes. APIs and defaults that
    assume MongoDB Atlas or a self-hosted replica set may not apply.

### Password rotation

DocumentDB's AWS-managed master password rotates every seven days by default.
See
[AWS-managed password rotation](https://docs.aws.amazon.com/documentdb/latest/devguide/docdb-secrets-manager.html).
Set `secret_rotation` to another number of days to change that schedule. Set
`secret_rotation=False` to disable automatic rotation. Then the password never
changes on its own. A leaked one stays valid until you rotate it by hand in
Secrets Manager.

!!! warning "Keep the AWS-managed password"
    Leave `manage_master_user_password` enabled (the default). Disabling it
    through customize means the default link cannot resolve a secret ARN, and
    Stelvio creates no rotation schedule, so `secret_rotation` has no effect.
    Stelvio rejects deferred values such as Pulumi `Output`. If you encrypt the
    secret with your own key (`master_user_secret_kms_key_id`), linked functions
    also need `kms:Decrypt` on that key. Add it with
    `db.link().add_permissions(...)`.

## Cost

Stelvio uses Standard storage (pay-per-use I/O). Prices below are on-demand in
`us-east-1`; check [AWS DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/)
and the [AWS Pricing Calculator](https://calculator.aws/#/createCalculator/DocumentDB)
for your region.

A default cluster bills:

- Instance: one `t3.medium` at **$0.078/hour**, about **~$57/month**
- Storage: **$0.10/GB-month**
- I/O: **$0.20 per million requests**
- Secrets Manager storage for the managed password: about **~$0.40/month**

The Function needs NAT or a Secrets Manager VPC endpoint to read the password.
See [VPC cost](vpc.md#cost).

## Customization

The `DocumentDb` component supports the `customize` parameter to override
underlying Pulumi resource properties. For an overview of how customization
works, see the [Customization guide](../../concepts/customization.md).

By default the AWS identifiers start with `{app}-{env}-{name}-` (instances
include the instance number before the trailing hyphen), and the Pulumi AWS
provider appends a unique suffix. You can override
`cluster_identifier`, `cluster_identifier_prefix`, `identifier`, or
`identifier_prefix` through customize. The identifier and prefix forms are
mutually exclusive for each AWS resource.

### Resource Keys

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `cluster` | [ClusterArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/cluster/#inputs) | The DocumentDB cluster |
| `instance` | [ClusterInstanceArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterinstance/#inputs) | Every cluster instance. Use a callable for per-instance values. |
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

!!! warning "Customizing `parameters` replaces the whole list"
    `parameter_group.parameters` replaces Stelvio's list rather than appending.
    TLS stays on either way because AWS enables it by default and Stelvio's
    default list sets `tls=enabled`. Add a `tls` entry only to change it.
    Intentionally disabling TLS also requires a corresponding link-property
    override for `connection_uri`, which always emits the default TLS URI.

!!! warning "Do not set inline security-group rules through customize"
    DocumentDB already manages a standalone ingress rule on the cluster
    security group. Mixing inline `ingress` / `egress` with standalone rules
    can overwrite rules or produce perpetual deployment differences. Add a
    separate `SecurityGroupIngressRule` that references
    `db.resources.security_group` instead. That grants network access only.
    The client still needs credentials.

!!! warning "Replacing `vpc_security_group_ids` drops the generated group"
    Stelvio always creates a cluster security group and attaches app-SG ingress
    to that generated group. A dict override of `cluster.vpc_security_group_ids`
    replaces the list, so the generated group is no longer on the cluster.
    Append with a callable if you need extra groups and want to keep generated
    ingress; replace the list only when you own ingress on the groups you supply.

## Next Steps

- [Working with VPC](vpc.md): Isolated subnets, NAT, and Lambda in a VPC
- [Working with Lambda Functions](lambda.md): Functions that connect to the cluster
- [Working with HTTP APIs](http-api.md): Routes that pass `vpc=` and `links=[db]`
- [Linking](../../concepts/linking.md): How Stelvio injects connection properties and IAM
- [Customization](../../concepts/customization.md): Override Pulumi resource properties
- [Tags](../../concepts/tags.md): Tag your resources
