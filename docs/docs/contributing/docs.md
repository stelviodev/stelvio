# Writing documentation

Docs are how users experience Stelvio before they run it. Three pages are worth reading
next to this page:

- `docs/docs/components/aws/vpc.md`: the component guide to copy. Defaults stated up front,
  a cost section, a warning admonition doing real work, customization with resource keys.
- `docs/docs/components/aws/queues.md`: the configuration patterns — options table, link
  properties and permissions tables.
- `docs/docs/components/aws/lambda.md`: a deep feature page (packaging, dependencies,
  layers) kept navigable by its section structure.

## Where docs live

Everything is under `docs/docs/`:

- `intro/`: install, quickstart, CLI, project structure, troubleshooting.
- `concepts/`: cross-component ideas — linking, environments, state, customization, tags.
- `components/aws/`: one guide per component.
- `contributing/`: these pages.

`uv run zensical serve` builds and serves the site locally. Navigation is `[[project.nav]]`
blocks in `zensical.toml`, one per top-level section, paths relative to `docs/`:

```toml
[[project.nav]]
Components = [
    { "AWS" = [
        { "Vpc" = "docs/components/aws/vpc.md" },
    ] },
]
```

Because of how the website is built, only pages under `docs/docs` publish — anything
elsewhere renders in local `zensical serve` and never reaches stelvio.dev.

## The component guide shape

The sections, in order, with the live example to copy:

1. **Title**: "Working with X in Stelvio".
2. **Intro**: what it is, a link to the AWS docs, and what Stelvio creates by default. If
   it bills while idle, warn here, not in a footnote — vpc.md opens with a "NAT costs
   money" admonition.
3. **Creating X**: the smallest working example.
4. **Features**: one section per parameter or feature, each leading with a code example;
   prose explains after. Add an options table (Option | Default | Description) on top
   when there are several flat options worth scanning (queues.md); vpc.md has none, its
   two parameters each carry a section.
5. **Cost**: its own section when the component bills while idle — NAT today; containers,
   EC2, DocumentDB as they arrive. Real monthly numbers and a link to AWS pricing
   (vpc.md). Pay-per-use serverless components skip it.
6. **Linking**: Link Properties and Link Permissions tables (queues.md, topics.md).
7. **Customization**: a pointer to the [customization concept page](../concepts/customization.md),
   then a Resource Keys table — key, Pulumi Args type linked to the Pulumi registry,
   description — and one example (vpc.md).
8. **Next Steps**: links to the related concept pages.

Concept pages are freer in form, but the same rule holds: lead with code, explain after.

## Tone

Explain to a smart colleague. Friendly, direct, concrete.

- No marketing language ("powerful", "simple", "blazing fast"). Say what it does and what
  it costs; readers judge for themselves.
- Stelvio API names, not the underlying AWS, ASL, or Pulumi names: `state_timeout`, not
  `TimeoutSeconds`. A one-time "maps to X" when introducing a parameter is fine; don't
  re-surface it at every mention.
- One good example beats three similar ones. If a second example doesn't show a different
  behavior, cut it.
- No comparisons with other frameworks.
- What the user needs to know, not how Stelvio implements it. Internals earn a place
  when they affect the user: lambda.md explains how packaging works because that decides
  what ends up in the zip.

## Zensical mechanics

Admonitions carry detail that would break the flow of the main text — a naming rule AWS
enforces, a cost warning, a planned feature:

```markdown
!!! warning "NAT costs money"
    A VPC itself is free, but NAT is not — the default two-AZ setup with
    `nat="managed"` costs about $73/month plus data charges.
```

Every code block declares its language. Tables are for enumerable facts: configuration
options, link properties, resource keys, filter syntax.

One rendering footgun: a list directly after a paragraph needs a blank line before it or
it renders as part of the paragraph.

## Verify everything

Every example must work against the current codebase. Before a docs PR:

- Run the imports; `from stelvio.aws.vpc import Vpc, NatConfig` must be real.
- Check every parameter, property, and method named in prose and tables exists, with the
  documented default.
- Placeholders look real: `functions/orders.handler`, `my-queue`, `YOUR_PROFILE_NAME` —
  never `xxx` or `TEXT`.

Stale docs teach users wrong; a missing page teaches them nothing. Wrong is worse.

## Checklist

For a new component, the docs work is: the guide page, its `zensical.toml` nav entry, the
component list in `README.md`, and a changelog entry. This is the same list the
[component checklist](components.md#checklist) ends with — that page hands off to this one.
