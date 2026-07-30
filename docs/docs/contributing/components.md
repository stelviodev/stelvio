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

Before naming a resource, know where the string ends up. Three destinations:

1. **Pulumi state**: the logical name, the resource's first constructor arg. Always exists,
   no limit to worry about.
2. **AWS physical name**, if the resource has one, in one of two ways. Don't set the
   resource's `name` arg and Pulumi derives it from the logical name plus a random suffix.
   Set `name=` yourself and the string goes to AWS exactly as is (Topic does, because FIFO
   names must end in `.fifo`).
3. **The `Name` tag**: some resources (VPC, subnets, gateways) have no AWS name at all. The
   human-readable name is a tag, and tag values cap at 256.

`safe_name(prefix, name, max_length, suffix, pulumi_suffix_length)` builds the string for
all of these: app-env prefix plus your name, and when that would blow `max_length` it
truncates the name's tail and stamps a 7-char hash to keep it unique. Pick params from the
destination:

- `max_length`: the limit where the string lands. The AWS name limit for the resource type
  (64 for EventBridge rules, 256 for SNS topics), or 256 when it only lands in a tag.
- `pulumi_suffix_length`: 8 (default) when Pulumi will append its random suffix, meaning
  the resource has an AWS name you didn't set explicitly. 0 otherwise.
- `suffix`: anything that must survive truncation intact; it's re-appended after the hash.
  `.fifo` is the case that forced the param.

Repeated `safe_name` calls with the same params are worth a local helper (Vpc's
`_safe_name`). DRY applies here like everywhere.

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
- Docs page with a `zensical.toml` nav entry, README component list, changelog entry.
