# Stelvio agent skills

Skills and marketplace manifests for coding agents that build applications with
[Stelvio](https://stelvio.dev/).

This repository is published from `agent-support/marketplace/` in
[stelviodev/stelvio](https://github.com/stelviodev/stelvio). Pin a release tag that
matches your installed Stelvio version (for example `#v0.10.0`).

## Install

### CLI (recommended when you already use Stelvio)

```bash
pip install stelvio
stlv agents install --target cursor,codex,claude,opencode
```

Project installs write shared skills under `.agents/skills/` (Cursor, Codex,
OpenCode) and Claude skills under `.claude/skills/`. Pass `--global` for the
user-wide directories. After upgrading the `stelvio` package, run
`stlv agents update --target …`.

### Claude Code

```text
/plugin marketplace add stelviodev/agents
/plugin install stelvio@stelvio-agents
```

### Cursor

Load this repository through `.cursor-plugin/marketplace.json` (Customize, or a
team marketplace import). Public listing on cursor.com/marketplace is separate.

### Codex

Repo marketplace: `.agents/plugins/marketplace.json`. Install the `stelvio`
plugin from that marketplace after cloning or adding the repo.

### `npx skills`

```bash
npx skills add stelviodev/agents
```

Skills live under `plugins/stelvio/skills/*/SKILL.md`. If the CLI depth limit
misses them, use the plugin subpath:

```bash
npx skills add stelviodev/agents/plugins/stelvio
```

## Layout

```text
plugins/stelvio/
├── .claude-plugin/plugin.json
├── .cursor-plugin/plugin.json
├── .codex-plugin/plugin.json
└── skills/
    └── stelvio-best-practices/
        └── SKILL.md
```
