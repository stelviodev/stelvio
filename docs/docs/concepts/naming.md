# Resource Naming

Stelvio names every AWS resource for you. Knowing the shape helps when you look at the
AWS console, write an IAM policy by hand, or wonder why a rename created a new resource.

## The shape

```
<app>-<env>-<component>-<random>
```

For an app `shop`, environment `prod`, and `Queue("orders")`:

```
shop-prod-orders-a1b2c3d
```

The app and environment prefix keeps environments apart in one AWS account. The random
7-character tail is added by Pulumi when the resource is created. It lets a replacement
(a setting AWS can't change in place) create the new resource before deleting the old
one, with no name collision and no gap where the name doesn't exist.

FIFO queues and topics end in `.fifo` after the random tail: `shop-prod-orders-a1b2c3d.fifo`.

## Deterministic names

Three resources carry no random tail, because the provider needs the name up front or
the name itself is the grouping key:

| Resource | Name |
|----------|------|
| `Layer` | `<app>-<env>-<name>` (every version publishes under this one layer name) |
| `IdentityPool` | `<app>-<env>-<name>` |
| `Email` configuration set | `<app>-<env>-<name>-config-set` |

## Long names

AWS limits name length per service: 63 for S3 buckets, 80 for SQS queues, 128 for Cognito
user pools. When the prefix plus your component name would overflow, Stelvio keeps the
head of the name and replaces the tail with a 7-character hash, so the name stays unique
and within the limit. Keep app, environment and component names short and the hash never
appears.

## Renaming a component

Changing a component's name in `stlv_app.py` is a new resource to Pulumi, not a rename.
The next deploy creates the new one and deletes the old. The app or environment name works
the same way, see [Renaming](state.md#renaming). Where that bites:

- `Bucket`: the old bucket keeps its objects, and the delete fails while it is non-empty.
  Move or delete the data first.
- `UserPool`: users live in the pool. The new pool starts empty.
- `DynamoTable`: the data goes with the table.
- `Queue`: in-flight messages are lost with the old queue.

Read the `stlv diff` output before a deploy that shows a delete on any of these.
