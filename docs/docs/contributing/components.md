# Writing components

A component wraps a group of AWS resources behind one Python class. Users construct it, link
it, read `.resources`. Three files are worth reading next to this page:

- `stelvio/aws/cron.py`: the shape to copy. Validation, handler parsing, a wrapped Function.
- `stelvio/aws/topic.py`: linking, properties, child components.
- `stelvio/aws/vpc.py`: many resources, per-resource customization and tags.

## In Pulumi terms

`Component` extends Pulumi's `ComponentResource`. The type string passed to
`super().__init__`, `"stelvio:aws:Cron"`, becomes its URN type, and everything created with
`self._resource_opts()` is parented under it in state:

```
stelvio:aws:Cron (nightly-report)
├── aws:cloudwatch/eventRule:EventRule
├── aws:cloudwatch/eventTarget:EventTarget
├── aws:lambda/permission:Permission
└── stelvio:aws:Function (nightly-report-fn)
    ├── aws:iam/role:Role
    ├── aws:iam/rolePolicyAttachment:RolePolicyAttachment
    └── aws:lambda/function:Function
```

This tree is what the CLI groups deploy output by. `_resource_opts()` also adds an alias
from the stack root, so apps deployed before a resource moved into a component migrate
without replacement.

## The shape

Two halves. `__init__` validates and stores, `_create_resources()` creates and returns.
The component registers itself with Pulumi on construction; the AWS resources don't exist
until Stelvio reads `.resources` at deploy time.

```python
class Cron(Component[CronResources, CronCustomizationDict]):
    def __init__(self, name, schedule, ..., *, tags=None, customize=None):
        super().__init__("stelvio:aws:Cron", name, tags=tags, customize=customize)
        _validate_schedule(schedule)
        self._schedule = schedule

    def _create_resources(self) -> CronResources:
        rule = cloudwatch.EventRule(...)
        ...
        return CronResources(rule=rule, target=target, permission=permission, function=fn)
```

The rules:

- Validate in `__init__` with module-level pure functions. Raise `ValueError` or `TypeError`
  naming the bad value and the accepted shapes. Fail when the object is constructed, not
  halfway through a deploy; what needs a cloud lookup (Vpc's AZ check) is the exception.
- `_create_resources()` is pure: reads `self._x`, sets nothing, returns everything.
- Resources land in a frozen `@final` dataclass named `{Component}Resources`. Expose what a
  user might reference; machinery (route table associations) stays out.
- Every component's `__init__` must have keyword-only `customize`, plus `tags` unless
  nothing it creates supports tags on AWS (`TopicQueueSubscription`), and `parent` when
  other components use it (like `Function`). Mark the class `@final`.

## The four dataclasses

A component brings up to four supporting types, named by convention:

- `{X}Resources`: what `_create_resources()` returns. Always.
- `{X}CustomizationDict`: the valid `customize=` keys, one per resource. Mirrors
  `{X}Resources` fields (singular where those are lists); a shared test keeps them in sync.
- `{X}Config` and `{X}ConfigDict`: when a component takes too many extra params. The
  constructor already carries `name`, `tags`, `customize`; two or three extras are the max
  (Cron's `schedule`, `enabled`, `payload`), over that, group them into a dataclass with a
  plain-dict twin (`NatConfig`, `DynamoTableConfig`). Validate in `__post_init__`, normalize
  dict-or-dataclass once in `__init__`, so the rest of the code sees one type. Keep the twins
  in sync with `assert_config_dict_matches_dataclass` in the component's tests.

They live in the component's file; `function/` splits into modules only because of size.

## Child resources

Every Pulumi resource gets `opts=self._resource_opts()`: parent, provider, and migration
alias in one place. `depends_on` goes through it too. Don't build `ResourceOptions` by hand.

Child Stelvio components (a wrapped `Function`) instead take `parent=self`, `tags=self.tags`,
and their slice of customization: `customize=self._customize.get("function")`.

Components are the user-facing units; Pulumi resources are the machinery Stelvio runs for
you. Vpc's `Route` and `RouteTableAssociation` are machinery, so they stay hidden, not even
in `VpcResources`. `TopicSubscription` is a component for an architectural reason: at deploy
Stelvio creates every registered component's resources independently, so each subscription
is its own unit. Its `Function`, subscription and permission come up on their own;
`Topic._create_resources()` never knows how many subscriptions exist. Own unit also means
own customization, tags, and group in deploy output.

Several children of one Pulumi type print with a suffix in `stlv diff`/`deploy`: the logical
name minus app, env and component prefix, `Subnet (public-subnet-a)`. Register
`@child_label("Vpc")` next to the class to shorten it: it gets that short name and returns the
label (Vpc: `public-subnet-a` becomes `public-a`); falsy keeps the short name. Lone children get
no suffix.

## Customization and tags

Every resource's args go through the customizer:

```python
rule = cloudwatch.EventRule(
    rule_name,
    **self._customizer("rule", {"schedule_expression": self._schedule}, inject_tags=True),
    opts=self._resource_opts(),
)
```

Declare the keys in a `CustomizationDict` TypedDict (the second type parameter); the base
class validates them. Merge is shallow, the user's value replaces yours. `inject_tags=True`
on taggable resources only, and keep it at the callsite where you can see it.

## Naming

Before naming a resource, know where the string ends up. Three destinations, three
recipes:

1. **Pulumi state, AWS derives the rest.** The logical name is the resource's first
   constructor arg. Most resources with an AWS name go this way: don't pass `name=`, and
   pulumi-aws derives the AWS name from the logical name plus an 8-char random suffix
   (`Queue`, `Topic`, `Bucket`, `DynamoTable`, `UserPool`, `UserPoolClient`, the
   CloudFront function in `S3StaticWebsite`). The suffix is what makes replacements safe:
   the new resource never collides with the one being deleted.
2. **Deterministic AWS name.** Only when the provider requires the name input
   (`IdentityPool`, `LayerVersion`, the SES configuration set in `Email`). Pass one string
   as both the logical name and the name input, built with `pulumi_suffix_length=0`.
3. **The `Name` tag.** VPC family resources have no AWS name at all. The human-readable
   name is a tag, and tag values cap at 256.

`resource_name(base, *, limit, suffix="", pulumi_suffix_length=8)` in `stelvio.component`
builds the string for the first two: app-env prefix plus your base, and when that would
blow `limit` it truncates the base's tail and stamps a 7-char hash. It wraps
`safe_name(prefix, name, max_length, suffix, pulumi_suffix_length)`, the older form that
takes the prefix explicitly; that one still serves the tag destination and most existing
sites. Any AWS-facing name built without either is a bug waiting for a long app name.

- `limit`: where the string lands. The AWS limit for the resource type (63 for buckets,
  128 for user pools), or the provider's own cap when that is lower: pulumi-aws cuts SQS
  and SNS autonames at 80 even though SNS allows 256. For tag-only names, 256.
- `pulumi_suffix_length`: keep the default 8 when Pulumi appends its suffix (recipe 1), so
  the final AWS name still fits. 0 for recipes 2 and 3.
- `suffix`: a tail that must survive truncation; it is re-appended after the hash. Only
  needed for names Stelvio sets itself. For autonamed FIFO queues and topics the provider
  appends `.fifo` after its random suffix, so the component strips `.fifo` from the
  logical name instead and `Queue` reserves 5 chars (`limit=MAX_QUEUE_NAME_LENGTH -
  len(".fifo")`); `Topic` has room to spare (80 + 8 + 5 < 256).

Pulumi rejects a logical name that overflows the limit at preview time, so the guard in
recipe 1 is load-bearing, not cosmetic. Repeated same-param calls are worth a local helper
(Vpc's `_safe_name`).

## Linking

If other components will link to yours, add `LinkableMixin` and a default creator:

```python
@link_config_creator(Topic)
def default_topic_link(topic: Topic) -> LinkConfig:
    t = topic.resources.topic
    return LinkConfig(
        properties={"topic_arn": t.arn, "topic_name": t.name},
        permissions=[AwsPermission(actions=["sns:Publish"], resources=[t.arn])],
    )
```

Properties become `STLV_` env vars on the linked Function, and typed accessors in the
`stlv_resources.py` Stelvio generates into its Lambda package. Permissions become IAM
statements on its role. Least privilege: the actions a user of the component needs, not
`sns:*`. Not everything links; Vpc has no creator because there's nothing to call and
nothing to permit.

## Public surface

Two kinds of properties belong on the class:

- Shortcuts to resource outputs users wire elsewhere: `topic.arn` is
  `self.resources.topic.arn`. Same for `url`, `stream_arn`, names.
- The parsed config, as one `config` property (DynamoTable), not a mirror property per
  field. A field shortcut like `partition_key` only when it earns its traffic.

`self.register_outputs({...})` keys show in the CLI after deploy. The bar is high: so far
only URLs, `{"url": url}`, the one thing a user goes looking for. Most components skip the
call.

## Checklist

Code: validation, `_create_resources`, `_resource_opts` everywhere, customization keys,
tags, `safe_name`, link creator if linkable. Then the part that gets forgotten:

- Export from the package `__init__.py`.
- Unit tests plus the four shared suites (see [Writing unit tests](unit-tests.md)), and
  integration tests.
- Creates persistent data? Add its types to `_DATA_LOSS_REPLACEMENT_TYPES` in
  `stelvio/rich_deployment_model.py` so replacements warn before eating data.
- New resource type? Add it to `RESOURCE_TYPE_NAMES` in the same file or the CLI prints the
  raw Pulumi type.
- Docs page with a `zensical.toml` nav entry, README component list, changelog entry.
