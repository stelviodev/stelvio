# Working with VPC (Virtual Private Cloud) in Stelvio

The `VPC` component lets you use AWS VPC in Stelvio.
It's useful if you need to use resources that require VPC.

By default, VPC component creates VPC with two AZs (availability zones) with
three subnets in each and Internet Gateway.

Three types of subnets are: public, private and isolated. It also creates route
tables and security groups following best practices.

(We could describe more details but probably not here but maybe in some
details section towards the end).

## Creating VPC

Here's how to create VPC with default settings:

```py
from stelvio.aws.vpc import Vpc

vpc = Vpc("main")

```

This will create VPC with two AZs and with 3 subnets in each.

## AZs - Availability Zones

If you need more AZs you can use `az` parameter, it takes a number. Remember
that each AWS region has different number of AZs so make sure you'll not set
number higher than available AZs in your region.

```py
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", az=3)
```
`az` param also accepts list of AZ names if you want to be specific: `az=["us-east-1b", "us-east-1c"]`

## NAT

NAT allows resources in your private subnets to access internet. Without NAT
private and isolated subnets are same - can't access anything outside of VPC.
By default, NAT is not enabled because it incurs cost.

Stelvio gives you two options for NAT: managed and ec2.

Managed NAT, as name suggests, is managed by AWS, it autoscales with your needs
but it isn't cheap. ec2 NAT is small ec2 instance managed by Stelvio that is much
cheaper. [See Cost section below](#cost).

```py
from stelvio.aws.vpc import Vpc

vpc = Vpc("main", nat="managed")
```

This will create one NAT in each AZ. That's generally good practice for production in case one AZ goes down but for non production environments 
one shared NAT is enough. To use only one NAT per VPC use `NatConfig`:

```py
from stelvio.aws.vpc import Vpc, NatConfig

vpc = Vpc("main", nat=NatConfig(type="managed", single=True))


```
`NatConfig` allows you to also configure additional details:

- `ip` - list of Elastic IP allocation IDs, you must provide same number of ids as you have AZs, unless you set `single=True`.
- `ami` - AMI to use for ec2 NAT; default is `fck-nat`.
- `instance` - which ec2 instance type to use; default is `t4g.nano`
- `role` - IAM role if you want to reuse some; by default new role is created

## Cost

TODO: Rewrite this better. But core info is below.

Simple math for now: Managed NAT is ~$32/month per NAT + $0.045/GB. 
ec2 with t4g.nano is ~$3/mo per NAT + $0.09/GB.
With 2 AZs it's $64 per month + data for managed and $6 per month + data for ec2.

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

