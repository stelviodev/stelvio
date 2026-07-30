# Writing unit tests

Unit tests run Pulumi with mocked providers: nothing deploys, no AWS credentials, the suite
runs in seconds. `uv run pytest`.

AWS component tests live in `tests/aws/`, core tests in `tests/` itself, plus `tests/dns/` and
`tests/bridge/`. Integration tests (`tests/integration/`) deploy real resources and gate
releases: every component has them, add them for anything new, run them if you can. See
[Writing integration tests](integration-tests.md).

Read `tests/aws/test_vpc.py` next to this page. It's the file the others are converging on.

## Test behavior, not implementation

That's Kent Beck's line and it's the whole idea. Build the component the way a user does,
constructor and `.resources`, nothing below. Then assert what that produced. Non-component
code follows the same rule: feed inputs at the highest practical seam, assert
user-observable output.

```python
def test_vpc_raises_type_error_when_nat_wrong_type():
    error = "'nat' must be 'managed', a NatConfig, a dict, or None."
    with raises(TypeError, match=re.escape(error)):
        Vpc("main_vpc", nat=42)
```

The check could also target the helper that does the validating:

```python
    with raises(TypeError, match=re.escape(error)):
        _normalize_nat(42)
```

Both pass today. Only the `Vpc` one survives renaming `_normalize_nat`, inlining it, or
moving the check into `NatConfig.__post_init__`. Same rule for config dataclasses: drive
validation through `Vpc(nat={"type": "ec2"})`, not `NatConfig(type="ec2")`. Config objects are
fine as inputs, not as the thing under test.

What a user can see:

- **Created resources**: type, inputs, how they reference each other. Including resources
  `.resources` never exposes (`Route`, `RouteTableAssociation`): the test doesn't create
  them and Python can't reach them, but they land in the user's AWS account. Behavior, not
  implementation; `assert_res_counts` is what keeps them guarded.
- **Validation errors**: exception type and message.
- **Public surface**: `.resources` and properties like `topic.arn`, where they resolve from a
  created resource. A property echoing config back is plumbing, skip it.
- **Links**: `STLV_` env vars and IAM statements on a linked `Function`.

Everything else is implementation. If you're reaching into `ComponentRegistry._instances`, the
assertion you want is on a created resource.

**The invariant: no behavior may be guarded only by a test that reaches below the public
API.** When the behavioral tests are a complete net, refactors stay green. A below-API test
whose behavior is already pinned behaviorally is redundant: delete it.

Prove redundancy by mutation, not coverage. Break the behavior, run the suite, watch something
fail, restore. If nothing fails you've found a gap (add the behavioral test) or a genuine
exception (keep the test, say why). Coverage proves a line ran, not that anything asserted the
result. We've had a mutation survive at 94%.

Beck's [Test Desiderata](https://testdesiderata.com/) is the longer yardstick. Three of the
twelve matter most here:

- **Structure-insensitive**: what implementation-coupled tests break. The point of this page.
- **Specific**: the price you pay. A public-API test says less about where it broke; earn it
  back with good names and narrow assertions, not with a unit test.
- **Predictive**: mocks return plausible values, not AWS's. Green means we asked for what we
  intended, not that AWS accepts it. That's what integration tests are for.

## Writing a test

`clean_registries` and `app_context` are autouse: clean registry, app `test`, env `test`,
region `us-east-1`. That's where the `TP` prefix (`"test-test-"`) comes from.

Beyond that, take what you need. Constructor validation needs no fixtures, like the test
above. `pulumi_mocks` when you assert on created resources, or when validation fires at deploy
(VPC's AZ check needs the region lookup). `project_cwd` when a `Function` is involved, its
handler file has to exist on disk.

Creating Pulumi resources requires `@pulumi.runtime.test`, in one of two shapes. Decorate the
test itself and return `Output.all(...).apply(check)` when the asserts ride on outputs of
resources you hold:

```python
@pulumi.runtime.test
def test_vpc_resources_parented_to_vpc_component(pulumi_mocks):
    r = Vpc("main_vpc").resources

    def check(urns):
        for urn in urns:
            assert "::stelvio:aws:Vpc$" in urn

    return pulumi.Output.all(r.vpc.urn, r.internet_gateway.urn).apply(check)
```

Every output the check reads goes inside the `Output.all`. The check runs when the listed
outputs resolve; one you read but didn't list may not have settled yet, and the test goes
flaky.

Decorate an inner `deploy()` when you assert on what the mocks recorded: inputs, counts, or
resources that never reach the `Resources` dataclass (`RouteTableAssociation`, `Route`). Those
are only complete after settlement, and `deploy()` returns after it, so the asserts are plain
code:

```python
def test_vpc(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc").resources

    deploy()

    pulumi_mocks.assert_res("main_vpc-igw", R.INTERNET_GATEWAY, {
        "vpcId": tid(TP + "main_vpc"),
        "tags": {"Name": TP + "main_vpc-igw"},
    })
    pulumi_mocks.assert_res_counts({
        R.VPC: 1, R.INTERNET_GATEWAY: 1, R.SUBNET: 6,
        R.ROUTE_TABLE: 6, R.ROUTE_TABLE_ASSOCIATION: 6,
    })
```

## Asserting resources

`assert_res(name, typ, inputs)`: exactly one resource named `TP + name`, full input compare.
Keys are camelCase: the component passes `cidr_block`, Pulumi records `cidrBlock`, and the
mocks hold what Pulumi recorded — assert `"cidrBlock"`. `tid(name)` is the mock's id for a resource, `R` the
type tokens. `partial=True` compares only the keys you list; the full compare is what catches
the input you didn't mean to send, so prefer it. JSON-string inputs compare parsed,
`json.loads(inputs["input"]) == payload`, never string to string.

`assert_res_counts` seals the test: exact counts per type, anything undeclared fails. **Assert
all the resources, not just the ones you're testing**, otherwise everything outside your three
assertions is unguarded. `assert_no_res(*types)` is the negative form.

Older suites use `created_*` helpers returning lists you count by hand. Read them, don't write
them.

## Parametrize what varies

Tests that differ only in inputs and expected values are one parametrized test:

```python
@mark.parametrize(("az", "error_message"), [
    param(0, "When `az` is a number it must be at least 1, got 0", id="zero"),
    param([], "When `az` is a list, you must provide at least one name.", id="empty-list"),
])
def test_vpc_raises_value_error_when_az_invalid(az, error_message):
    with raises(ValueError, match=re.escape(error_message)):
        Vpc("main_vpc", az=az)
```

Add an `id` only when the values don't read well on their own. `0` and `[]` are fine, a long
error string isn't.

Don't merge tests that only look alike, and don't add a column that repeats the input.
Repeating test *data* is fine, it's duplicated *logic* we're removing.

Three habits reviewers check: import helpers directly, `from pytest import mark, param,
raises`. Always give `raises` a `match`, a bare `raises(ValueError)` passes on the wrong
error. And no message on a plain `assert x == y`, pytest already prints both sides — add
context only when the failure couldn't say which resource it was.

## Shared suites

A new component also registers in four cross-component suites. Reviewers check all four:

- `tests/aws/test_tagging_contract.py`: `tags=` lands on every taggable resource.
- `tests/aws/test_customization_sync.py`: customize keys stay in sync with `Resources` fields.
- `tests/aws/test_keyword_only_constructor_params.py`: `tags` and `customize` are keyword-only.
- `tests/test_type_urns.py`: the canonical URN list, with its count.
