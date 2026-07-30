# Working with VPC in Stelvio

Stelvio supports creating [Amazon VPC (Virtual Private Cloud)](https://aws.amazon.com/vpc/) networks using the `Vpc` component. You need a VPC when you work with resources that live inside a private network — databases, or Lambda functions that access them.

By default, the `Vpc` component creates a VPC spanning two AZs (availability zones) with three subnets in each — public, private and isolated — plus an Internet Gateway and a route table per subnet, following best practices. Optionally it creates [NAT](#nat) so resources in private subnets can reach the internet.

!!! warning "NAT costs money"
    A VPC itself is free, but NAT is not — the default two-AZ setup with `nat="managed"` costs about $73/month plus data charges. See [Cost](#cost) before enabling NAT.

## Creating VPC

Here's how to create a VPC with default settings:

```python
from stelvio.aws.vpc import Vpc

vpc = Vpc("main")
```

This creates a VPC with two AZs and three subnets in each.

## AZs - Availability Zones

If you need more (or fewer) AZs, use the `az` parameter — it takes a number.
Stelvio picks the first N available AZs in your region:

```python
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", az=3)
```

`az` also accepts a list of AZ names if you want to be specific: `az=["us-east-1b", "us-east-1c"]`.

Each AWS region has a different number of AZs. Stelvio validates `az` during
deployment — asking for more AZs than the region has, or for an AZ name that
doesn't exist there, raises an error.

## Subnets and Network Layout

Each AZ gets one subnet of each type:

- **Public** — has a route to the Internet Gateway. For resources that must be reachable from the internet. This is also where NAT lives.
- **Private** — not reachable from the internet; outbound access only through [NAT](#nat). This is where your workloads — like Lambda functions — go.
- **Isolated** — no internet route at all. Ideal for databases.

The VPC uses the `10.0.0.0/16` network and carves subnet ranges from it automatically:

| Subnet   | Size | CIDR ranges (per AZ)              | Internet route       |
|----------|------|-----------------------------------|----------------------|
| Public   | /24  | `10.0.0.0/24`, `10.0.1.0/24`, …   | via Internet Gateway |
| Private  | /22  | `10.0.20.0/22`, `10.0.24.0/22`, … | via NAT (if enabled) |
| Isolated | /24  | `10.0.60.0/24`, `10.0.61.0/24`, … | none                 |

Private subnets are bigger (/22, ~1,000 IPs each) because that's where most of
your resources — and their network interfaces — end up.

Every subnet gets its own route table, and the VPC has DNS support and DNS
hostnames enabled.

## NAT

NAT allows resources in your private subnets to access the internet. Without NAT,
private and isolated subnets are the same — they can't access anything outside of
the VPC. By default, NAT is not enabled because it [incurs cost](#cost).

Stelvio currently supports managed NAT — NAT Gateways managed by AWS that
autoscale with your needs. It isn't cheap, though; [see Cost section below](#cost).

```python
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")
```

This will create one NAT in each AZ. That's generally good practice for production
in case one AZ goes down, but for non-production environments one shared NAT is
enough. To use only one NAT per VPC use `NatConfig`:

```python
from stelvio.aws.vpc import Vpc, NatConfig

vpc = Vpc("main", nat=NatConfig(type="managed", single=True))
```

You can also pass a plain dict: `nat={"type": "managed", "single": True}`.

!!! note "Planned: ec2 NAT"
    A much cheaper NAT option — a small EC2 instance running
    [fck-nat](https://fck-nat.dev) managed by Stelvio — is planned.
    `NatConfig` only accepts `type="managed"` for now.

<!-- Future (ec2 NAT, PR5) — original text, restore/adapt when it ships:

Stelvio gives you two options for NAT: managed and ec2.

Managed NAT, as name suggests, is managed by AWS, it autoscales with your needs
but it isn't cheap. ec2 NAT is small ec2 instance managed by Stelvio that is much
cheaper. [See Cost section below](#cost).

`NatConfig` ec2 options:

- `ami` - AMI to use for ec2 NAT; default is `fck-nat`.
- `instance` - which ec2 instance type to use; default is `t4g.nano`
- `role` - IAM role if you want to reuse some; by default new role is created
-->

### Using your own Elastic IPs

By default, Stelvio creates an Elastic IP for each NAT gateway. If you want to
keep your outbound IPs stable — for example because a third party whitelists
them — pass allocation IDs of your existing Elastic IPs via `ip`:

```python
vpc = Vpc(
    "main",
    nat=NatConfig(type="managed", ip=["eipalloc-0a1b2c3d", "eipalloc-4e5f6a7b"]),
)
```

You must provide exactly one allocation ID per NAT gateway: one per AZ, or a
single one with `single=True`. Stelvio then creates no Elastic IPs of its own —
the adopted IPs remain yours and are not released when the VPC is destroyed.

## Cost

The VPC itself — subnets, route tables, Internet Gateway — is free. NAT is what
costs money:

- A managed NAT Gateway costs **~$33/month** ($0.045/hour, always on) plus
  **$0.045 per GB** of data processed.
- Its Elastic IP adds **~$3.65/month** — since February 2024 AWS charges
  $0.005/hour for every public IPv4 address, in use or not.
- The default two-AZ setup with `nat="managed"` therefore runs **~$73/month**
  before data charges.
- `NatConfig(type="managed", single=True)` cuts that to one NAT — **~$37/month**.
  Good enough for dev, staging, and many small production apps.

Prices are for `us-east-1`; check [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/)
for your region.

<!-- Future (ec2 NAT, PR5) — cost comparison, restore when it ships:

ec2 with t4g.nano is ~$3/mo per NAT + $0.09/GB.
With 2 AZs it's $64 per month + data for managed and $6 per month + data for ec2.
-->

## Customization

The `Vpc` component supports the `customize` parameter to override underlying
Pulumi resource properties. For an overview of how customization works, see the
[Customization guide](../../concepts/customization.md).

### Resource Keys

| Resource Key            | Pulumi Args Type                                                                                                    | Description                     |
|-------------------------|---------------------------------------------------------------------------------------------------------------------|---------------------------------|
| `vpc`                   | [VpcArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/vpc/#inputs)                                    | The VPC                         |
| `internet_gateway`      | [InternetGatewayArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/internetgateway/#inputs)            | The Internet Gateway            |
| `public_subnet`         | [SubnetArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/subnet/#inputs)                              | Public subnets (all AZs)        |
| `private_subnet`        | [SubnetArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/subnet/#inputs)                              | Private subnets (all AZs)       |
| `isolated_subnet`       | [SubnetArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/subnet/#inputs)                              | Isolated subnets (all AZs)      |
| `public_route_table`    | [RouteTableArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/routetable/#inputs)                      | Public route tables (all AZs)   |
| `private_route_table`   | [RouteTableArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/routetable/#inputs)                      | Private route tables (all AZs)  |
| `isolated_route_table`  | [RouteTableArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/routetable/#inputs)                      | Isolated route tables (all AZs) |
| `elastic_ip`            | [EipArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/eip/#inputs)                                    | NAT Elastic IPs                 |
| `nat_gateway`           | [NatGatewayArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ec2/natgateway/#inputs)                      | NAT Gateways                    |

### Example

```python
vpc = Vpc(
    "main",
    customize={
        "public_subnet": {"map_public_ip_on_launch": True},
    }
)
```

!!! warning "Subnet and route table keys apply to all subnets of that type"
    There is one subnet (and route table) of each type *per AZ*, but only one
    customize key per type — a dict value is applied to every subnet of that
    type identically. Overriding per-subnet values like `cidr_block` this way
    breaks deployment, because every subnet of that type would get the same value.
    Use a callable instead — it's called for each subnet with that subnet's
    computed properties, so you can adjust values per subnet. For example, to
    move public subnets from `10.0.0.x` to `10.0.100.x` (each subnet's own CIDR
    shifted, staying within the VPC's `10.0.0.0/16`):

    ```python
    def shift_public_cidr(props):
        octets = props["cidr_block"].split(".")  # 10.0.0.0/24, 10.0.1.0/24, ...
        octets[2] = str(int(octets[2]) + 100)    # 10.0.100.0/24, 10.0.101.0/24, ...
        return {**props, "cidr_block": ".".join(octets)}

    vpc = Vpc("main", customize={"public_subnet": shift_public_cidr})
    ```

## Coming Soon

VPC support in Stelvio will grow in upcoming releases:

- **Components in VPC** — put Lambda functions (and other components) into your
  VPC with a simple `vpc=` parameter.
- **Automatic security groups** — [linking](../../concepts/linking.md) VPC
  resources will configure security groups for you.
- **Dev mode access** — reach resources inside your VPC from your local machine
  during `stlv dev`.
- **ec2 NAT** — much cheaper NAT using [fck-nat](https://fck-nat.dev) instances.

<!-- Future sections — drafts for upcoming PRs (Lambda-in-VPC, DocumentDB linking, dev-mode bastion). Uncomment/adapt as they ship.

## Adding components to VPC

Components that support VPC have vpc parameter in their init. 

```py
from stelvio.aws.vpc import Vpc
from stelvio.aws.function import Function

vpc = Vpc("main", nat="managed")

Function("my-function", handler="functions/my_function.handler", vpc=vpc)
```

Above code will put function `my-function` to VPC `main` and one of its private
subnets creating proper security group for it.

## Linking resources in VPC

When you link resources in VPC Stelvio creates and updates security groups automatically so resources can access other resources properly.

```py
from stelvio.aws.vpc import Vpc
from stelvio.aws.function import Function
from stelvio.aws.documentdb import DocumentDb

vpc = Vpc("main", nat="managed")

db = DocumentDb("my-doc-db", vpc=vpc)

Function("my-function", handler="functions/my_function.handler", vpc=vpc, links=[db])
```

I still need to figure out details about how to do this exactly and properly but
we'll have to update whole linking system for this to work probably.

## Dev mode

IMPLEMENTATION INFO:
For dev mode we'll also need to have `bastion` parameter to VPC. It will create
small ec2 instance in VPC (or reuse NAT instance if it's ec2) which then we can
connect to from local computer when in dev mode.
Stubs won't need to be in VPC, since those are just stubs and need to connect to AppSync. Bastion is needed for dev machine to reach VPC resources, not functions.

```py
from stelvio.aws.vpc import Vpc
from stelvio.aws.function import Function
from stelvio.aws.documentdb import DocumentDb

vpc = Vpc("main", nat="managed", bastion=True)

db = DocumentDb("my-doc-db", vpc=vpc)

Function("my-function", handler="functions/my_function.handler", vpc=vpc, links=[db])
```

-->

## Next Steps

- [Customization](../../concepts/customization.md) — Override any Pulumi resource property
- [Tags](../../concepts/tags.md) — Tag your VPC resources
