# Working with DocumentDB in Stelvio

Stelvio supports creating [Amazon DocumentDB](https://docs.aws.amazon.com/documentdb/) clusters using the `DocumentDb` component. DocumentDB is a MongoDB-compatible database that runs inside a VPC. Use it when you need Mongo-compatible queries, always-on capacity, and a VPC-bound data plane; [DynamoTable](dynamo-db.md) is the cheaper request-priced default for key-value data.

By default, `DocumentDb` creates a private, TLS-required, encrypted cluster in the given Vpc's isolated subnets, with an AWS-managed master password.

!!! warning "DocumentDB costs money"
    One `t4g.medium` instance is **~$50/month** idle. It bills while running, not per request, and each extra instance adds another. Functions that fetch the password from Secrets Manager also need `nat="managed"` on the Vpc (**~$37 to ~$73/month** extra). Prices are for `us-east-1`; check [AWS DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/) for your region.

## Creating a DocumentDB cluster

You need a `Vpc` with NAT, a folder handler that includes Amazon's CA bundle, and `pymongo`. Put the Function in the same Vpc and link it:

```python
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")
db = DocumentDb("todos", vpc=vpc)
Function(
    "api",
    handler="functions/todos::main.handler",
    requirements=["pymongo"],
    vpc=vpc,
    links=[db],
)
```

```text
stlv_app.py
functions/
    todos/
        main.py
        global-bundle.pem
```

Download the bundle before deploying:

```bash
mkdir -p functions/todos
curl --fail --output functions/todos/global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
```

The `::` folder form packages both `main.py` and `global-bundle.pem`. A single-file handler does not. Connect with TLS, `replicaSet="rs0"`, and `retryWrites=False` — see [Using the cluster from Lambda](#using-the-cluster-from-lambda).

`stlv destroy` deletes the data. See the warning under [What Stelvio sets](#what-stelvio-sets).

This creates a one-instance cluster on engine `5.0` in the Vpc's isolated subnets.

DocumentDB needs isolated subnets in at least two AZs, which the default `Vpc` has. `Vpc(..., az=1)`, or any Vpc with fewer than two isolated subnets, raises `ValueError` when the cluster is created.

!!! warning "The first deploy takes 10 to 20 minutes"
    AWS provisions the cluster and every instance before the deploy finishes, and destroying it is slow too. Budget the time; a stuck-looking deploy is usually just AWS.

You can pass the knobs as keyword arguments, as a `DocumentDbConfig`, or as a plain dict via `config=`, but not both kwargs and `config=` at once:

```python
from stelvio.aws.vpc import Vpc
from stelvio.aws.document_db import DocumentDb, DocumentDbConfig

vpc = Vpc("main", nat="managed")

# Keyword arguments
db = DocumentDb("todos", vpc=vpc, instances=2)

# Or using DocumentDbConfig
db = DocumentDb(
    "todos",
    config=DocumentDbConfig(vpc=vpc, instances=2),
)

# Or a dict
db = DocumentDb("todos", config={"vpc": vpc, "instances": 2})
```

### Configuration options

| Option                    | Default    | Description                                                                                          |
|---------------------------|------------|------------------------------------------------------------------------------------------------------|
| `vpc`                     | (required) | Existing `Vpc`. The cluster uses its isolated subnets.                                               |
| `instances`               | `1`        | Number of cluster instances (1 to 16).                                                               |
| `instance_class`          | `None`     | Instance class without the `db.` prefix. `None` uses `t4g.medium` unless customize supplies another. |
| `engine`                  | `"5.0"`    | Engine version: `"5.0"` or `"8.0"`.                                                                  |
| `deletion_protection`     | `False`    | Block `stlv destroy` / cluster deletion until flipped off and redeployed.                            |
| `backup_retention_period` | `7`        | Automated backup retention in days (1–35).                                                           |

`instance_class` is the size (`t4g.medium`); `instances` is how many. Passing AWS's `db.t4g.medium` form is rejected — Stelvio prepends `db.`.

Omitting `instance_class` (or setting it to `None`) uses `t4g.medium` from the instance resource defaults, so an app-wide `DocumentDb` instance customization can supply another class. An explicit `instance_class` wins over an app-wide dictionary default; a global callable is an override. Local customization wins over both.

Omitting `deletion_protection` or `backup_retention_period` (or setting them to `None`) uses `False` and `7` from the cluster resource defaults, so an app-wide `DocumentDb` cluster dictionary customization can supply other values. An explicit value wins over an app-wide dictionary default; a global callable is an override. Local customization wins over both.

```python
DocumentDb("todos", vpc=vpc, backup_retention_period=14, deletion_protection=True)
```

`vpc` is a `Vpc` instance, not a `VpcAttachment`. There is no subnet-tier picker and no user-supplied security-group list on DocumentDB.

### Instances

```python
from stelvio.aws.vpc import Vpc
from stelvio.aws.document_db import DocumentDb

vpc = Vpc("main", nat="managed")
db = DocumentDb("todos", vpc=vpc, instances=2)
```

### Engine version

```python
from stelvio.aws.vpc import Vpc
from stelvio.aws.document_db import DocumentDb

vpc = Vpc("main", nat="managed")
db = DocumentDb("todos", vpc=vpc, engine="8.0")
```

!!! warning "Changing engine upgrades the live cluster"
    Changing `engine="5.0"` to `engine="8.0"` requests an in-place major version
    upgrade with downtime. Stelvio sets `allow_major_version_upgrade=False`; an
    in-place bump must opt in with `customize={"cluster": {"allow_major_version_upgrade": True}}`.
    You must still satisfy the [AWS upgrade prerequisites](https://docs.aws.amazon.com/documentdb/latest/devguide/docdb-mvu.html).
    You cannot downgrade by changing `engine` back; recovery requires restoring
    a pre-upgrade snapshot into a new cluster.

For an existing cluster using the default `t4g.medium` instance:

1. Review the AWS prerequisites, apply required maintenance, create a manual
   snapshot, and rehearse the upgrade on a clone.
2. Keep `engine="5.0"`, change `instance_class="r6g.large"` (or `"r5.large"`), and deploy.
   Wait for the resize to finish before changing the engine. AWS requires scaling
   up burstable writers before the upgrade; changing both options in one deployment
   does not guarantee that the resize happens first.
3. Keep the larger instance, set `allow_major_version_upgrade=True` via customize,
   change `engine="8.0"`, and deploy for your planned maintenance window. Without
   `customize={"cluster": {"apply_immediately": True}}`, AWS may defer the change
   to that window. Verify the running engine version and test your application
   after the upgrade.
4. Wait for post-upgrade index metadata refresh and normal query performance before
   resizing again. Keep the pre-upgrade snapshot until you have verified recovery
   is no longer needed.

## What Stelvio sets

The component uses the following defaults and resource wiring. Options such as
port, encryption, and skip_final_snapshot can be overridden through
[Customization](#customization). Backup retention and deletion protection are
constructor options — see [Configuration options](#configuration-options). The
component places the cluster in the VPC's isolated subnets and admits traffic
from the shared app security group; linking alone does not change that network
wiring.

| Concern             | Value                                                                        |
|---------------------|------------------------------------------------------------------------------|
| Subnets             | Isolated subnets of `vpc` (at least two AZs)                                 |
| Instance class      | `db.{instance_class}` (default `db.t4g.medium`)                              |
| Engine              | `engine="docdb"`, version `5.0.0` or `8.0.0`                                 |
| Username            | `stelvio`                                                                    |
| Password            | AWS-managed Secrets Manager secret                                           |
| TLS                 | Required (`tls=enabled` on the cluster parameter group)                      |
| Encryption          | On                                                                           |
| Port                | `27017`                                                                      |
| Backup retention    | 7 days                                                                       |
| Final snapshot      | Skipped on destroy                                                           |
| Deletion protection | Off                                                                          |
| Public access       | Off (instances sit in isolated subnets)                                      |
| Network             | TCP 27017 from the Vpc's shared app security group to the cluster SG         |

!!! warning "Destroying the cluster deletes your data, with no snapshot to go back to"
    Stelvio sets `skip_final_snapshot=True` and `deletion_protection=False`.
    `stlv destroy` therefore drops the database immediately. For anything you
    care about, set `deletion_protection=True` and keep a final snapshot through
    `customize`. Setting `skip_final_snapshot=False` also requires
    `final_snapshot_identifier`, or destroy fails.

For a cluster whose data must survive accidental deletion:

```python
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

Choose a final snapshot identifier that is not already in use in your account and
region. To deliberately destroy this cluster, first set `deletion_protection=False`
and deploy that change, keeping the snapshot settings. Then destroy it. The final
snapshot remains available for restoration and incurs storage charges until deleted.

## Networking

The cluster lives in the Vpc's isolated subnets. Creating `DocumentDb` opens TCP 27017
from that Vpc's [app security group](vpc.md#security-groups) to the cluster security
group. Functions that join the same Vpc with the default app group — `Function(vpc=vpc)` —
can reach the cluster on that port. See [Lambda Functions in VPC](vpc.md#lambda-functions-in-vpc).

Custom `security_groups` on `VpcAttachment` replace the app group. Those functions are
not admitted by DocumentDB's ingress; you wire their rules yourself.

A Function without `vpc=`, or in a different Vpc, has no network path to the cluster.

## Connecting from Lambda

Put the Function in the same Vpc and link it. The Function wears the Vpc's app security
group, which DocumentDB already admits on TCP 27017. Use a folder handler so Stelvio
packages the Amazon CA bundle alongside your code — the layout is in
[Creating a DocumentDB cluster](#creating-a-documentdb-cluster).

API routes take the same folder handler and options:

```python
from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")
db = DocumentDb("todos", vpc=vpc)
api = HttpApi("todos-api")
api.route(
    "GET", "/todos", "functions/todos::main.handler",
    requirements=["pymongo"], vpc=vpc, links=[db],
)
```

Keep the Function in private subnets with `nat="managed"` so it can call Secrets Manager
for the password. Isolated-subnet Functions can open TCP to the cluster, but need a
Secrets Manager interface VPC endpoint to fetch the secret. Stelvio does not create
that endpoint. Adding `nat="managed"` only routes private subnets through NAT; isolated
subnets remain isolated. Either provide the endpoint or move the Function to private
subnets with NAT.

`Function(links=[db])` without `vpc=` still injects env vars and `GetSecretValue`. It does
not create a network path. The same is true of a Function in a different Vpc, or one that
replaces the app group with custom `security_groups` on `VpcAttachment` — those groups are
not admitted by DocumentDB's ingress; you wire their rules yourself.

`stlv dev` cannot reach the cluster.

## Linking

Linking injects connection properties and grants `secretsmanager:GetSecretValue` on the
AWS-managed master-user secret. The password stays in Secrets Manager; it is never an
environment variable.

### Link Properties

For a cluster named `todos`:

| Property      | Environment variable       | Description                                      |
|---------------|----------------------------|--------------------------------------------------|
| `host`        | `STLV_TODOS_HOST`          | Cluster writer endpoint                          |
| `reader_host` | `STLV_TODOS_READER_HOST`   | Cluster reader endpoint                          |
| `port`        | `STLV_TODOS_PORT`          | Port (default `27017`)                           |
| `username`    | `STLV_TODOS_USERNAME`      | Master username (default `stelvio`)              |
| `secret_arn`  | `STLV_TODOS_SECRET_ARN`    | Secrets Manager ARN for the AWS-managed password |

### Link Permissions

Linked Lambda functions receive:

- `secretsmanager:GetSecretValue` on the AWS-managed master-user secret

DocumentDB authenticates with username and password. There are no DocumentDB data-plane
IAM actions.

Default linking requires the AWS-managed master-user secret. Setting
`customize={"cluster": {"manage_master_user_password": False}}` removes that
assumption: the default link raises an error when no managed secret is available.
Keep password management enabled when using `links=[db]`; supplying a password
yourself does not make it available through the default link.

If you `customize` `kms_key_id` on the cluster, add `kms:Decrypt` on that key to the
link — `GetSecretValue` alone cannot unwrap a customer-managed secret.

### Using the cluster from Lambda

In `functions/todos/main.py`, fetch the password and connect with TLS:

```python
import json
from pathlib import Path

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
        tlsCAFile=str(Path(__file__).with_name("global-bundle.pem")),
        replicaSet="rs0",
        retryWrites=False,
    )
    try:
        collection = client.todos.items
        collection.replace_one({"_id": "hello"}, {"_id": "hello", "ok": True}, upsert=True)
        return {"item": collection.find_one({"_id": "hello"})}
    finally:
        client.close()
```

!!! info "DocumentDB is not full MongoDB"
    TLS is required. Use `replicaSet="rs0"` and `retryWrites=False` — DocumentDB does not
    support retryable writes, and clients that omit the replica set name often fail to
    discover the cluster. APIs and defaults that assume MongoDB Atlas or a self-hosted
    replica set may not apply.

### Password rotation and connection reuse

DocumentDB rotates its managed password every seven days by default; the schedule
can be changed. See [AWS-managed password rotation](https://docs.aws.amazon.com/documentdb/latest/devguide/docdb-secrets-manager.html).

The example fetches current credentials and closes its client on each invocation.
For connection reuse across invocations, bound the credential cache lifetime and
handle authentication failures by fetching the current secret, closing the stale
client, and reconnecting once. A cache TTL alone cannot prevent stale credentials
immediately after a rotation. Do not automatically replay a write whose outcome
is unknown. Never log the secret or a connection string containing the password.

## Cost

DocumentDB instances are always on:

- One `t4g.medium` is **~$50/month** before storage and I/O.
- `instances=2` is two of those.
- Reaching Secrets Manager from a Function in this Vpc needs `nat="managed"`: **~$73/month** with the default of one NAT per AZ, or **~$37/month** with `nat=NatConfig(type="managed", single=True)`, plus data charges. See [NAT](vpc.md#nat) for the single-NAT form.

Destroy skips creation of a final snapshot by default. Existing manual and pre-upgrade
snapshots remain after teardown and can incur storage charges until you delete them.
Keep any snapshots you still need for recovery; skipping the final snapshot does not
clean up earlier backups.

Prices are for `us-east-1`; check [AWS DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/) and [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/) for your region.

## Customization

The `DocumentDb` component supports the `customize` parameter to override underlying Pulumi resource properties. For an overview of how customization works, see the [Customization guide](../../concepts/customization.md). App-wide dict customize is another default for that resource; a global callable is an override.

Default linking requires `manage_master_user_password=True`. If customization disables
it, the default link cannot resolve a managed secret ARN. See [Linking](#linking).

### Resource Keys

| Resource Key      | Pulumi Args Type                                                                                                       | Description                     |
|-------------------|------------------------------------------------------------------------------------------------------------------------|---------------------------------|
| `cluster`         | [ClusterArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/cluster/#inputs)                             | The DocumentDB cluster          |
| `instance`        | [ClusterInstanceArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterinstance/#inputs)             | Cluster instances (all of them) |
| `subnet_group`    | [SubnetGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/subnetgroup/#inputs)                     | Subnet group (isolated subnets) |
| `parameter_group` | [ClusterParameterGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/docdb/clusterparametergroup/#inputs) | Cluster parameter group         |
| `security_group`  | [SecurityGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/securitygroup/#inputs)                   | Cluster security group          |

### Example

```python
from stelvio.aws.vpc import Vpc
from stelvio.aws.document_db import DocumentDb

vpc = Vpc("main", nat="managed")
db = DocumentDb(
    "todos",
    vpc=vpc,
    customize={
        "cluster": {"preferred_backup_window": "07:00-09:00"},
    },
)
```

!!! warning "Availability zone changes are ignored after creation"
    AWS pads a DocumentDB cluster to three AZs. Stelvio omits `availability_zones`
    by default and ignores changes to that field to prevent replacement caused by
    AWS's padding. A value supplied through `customize` applies when the cluster is
    created, but subsequent changes are ignored, including intentional customization
    changes. Do not use this field to move an existing cluster between AZs.

!!! warning "`instance` applies to every instance"
    There is one customize key for all cluster instances. A dict value is applied to every instance identically. Use a callable if you need per-instance values, since it is called for each instance with that instance's computed properties.

!!! warning "Customizing `parameters` replaces the whole list"
    Customization is a shallow merge, so `parameter_group.parameters` replaces Stelvio's
    list rather than appending. Stelvio puts `tls=enabled` back unless your list already
    has a `tls` parameter. Include `tls` yourself if you want a different value.

### Adding network access

Do not set inline `ingress` or `egress` through `customize["security_group"]`.
DocumentDB already manages a standalone ingress rule on that group; mixing inline
and standalone rules can overwrite rules or produce perpetual deployment differences.
See [Pulumi's security group warning](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/securitygroup/).

Add a separate rule referencing `db.resources.security_group` instead. For example,
to admit an existing client security group in the same VPC:

```python
from pulumi_aws.vpc import SecurityGroupIngressRule

SecurityGroupIngressRule(
    "todos-client-ingress",
    security_group_id=db.resources.security_group.id,
    referenced_security_group_id=client_security_group.id,
    ip_protocol="tcp",
    from_port=db.resources.cluster.port,
    to_port=db.resources.cluster.port,
)
```

This grants network access only. The client still needs credentials and permission
to fetch the secret. If you use a custom AWS provider, pass the matching provider
in this rule's Pulumi resource options.

## Not exposed

These are not constructor options:

- Elastic clusters, serverless DCU, and global clusters
- Custom ports, usernames, or passwords as constructor args (ports and usernames are reachable through `customize={"cluster": {...}}`; the password stays AWS-managed)
- Database or collection names
- Auto-creating a `Vpc`
- Snapshot restore
- CloudWatch log exports as API
- VPC endpoints
- Bastion or `stlv dev` access to the cluster
- Per-function security groups

## Next Steps

- [Working with VPC](vpc.md) — Isolated subnets, NAT, and Lambda in a VPC
- [Linking](../../concepts/linking.md) — How Stelvio injects env vars and IAM
- [Customization](../../concepts/customization.md) — Override Pulumi resource properties
- [Tags](../../concepts/tags.md) — Tag your resources
- [Working with Lambda Functions](lambda.md) — Functions that will connect to the cluster
