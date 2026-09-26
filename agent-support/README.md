# Agent support

Maintainer guide for Stelvio’s **user-facing agent skills** and the **private
eval loop** used to develop and check them.

Users who install skills never see this directory’s evals. Skills live under
`marketplace/` and stay free of eval case ids, “Maps to” lines, and internal
contract labels. Evals stay in this repo under `evals/` and are not published.

Design and status: [`notes/stelvio-user-agent-plan-v2.md`](../notes/stelvio-user-agent-plan-v2.md).
Behavioral contract for the first skill:
[`notes/stelvio-best-practices-v0.md`](../notes/stelvio-best-practices-v0.md).

---

## What this is

**Agent support** means helping coding agents build *applications with*
Stelvio (components, links, `Resources`, subscribe/notify, `customize`) — not
agents that change the Stelvio framework itself.

Two trees:

| Path | Purpose | Published? |
|---|---|---|
| `marketplace/` | Skills + marketplace manifests | Yes → private [`stelviodev/agents`](https://github.com/stelviodev/agents) |
| `evals/` | Cases, graders, local sandbox, results | No (main repo only) |

Current skill: `marketplace/plugins/stelvio/skills/stelvio-best-practices/`.

The first eight cases (`e1`–`e8`) are a **development set**. They may shape
skill prose. Improvement on them shows the skill can change behavior; it does
**not** prove generalization. Broader claims need reserved, unseen tasks.

---

## Layout

```text
agent-support/
├── README.md                          # this file
├── marketplace/                       # split → stelviodev/agents root
│   ├── README.md                      # user/install-facing
│   ├── .claude-plugin/marketplace.json
│   ├── .cursor-plugin/marketplace.json
│   ├── .agents/plugins/marketplace.json
│   └── plugins/stelvio/
│       ├── .claude-plugin/plugin.json
│       ├── .cursor-plugin/plugin.json
│       ├── .codex-plugin/plugin.json
│       └── skills/<skill-name>/SKILL.md
└── evals/
    ├── cases/<case-id>/
    │   ├── prompt.md
    │   ├── fixture/
    │   ├── checks.toml
    │   └── solutions/{good,bad-*}/    # grader unit tests only
    ├── graders/
    ├── schema/result.schema.json
    ├── sandbox/run_local.py
    └── results/                       # gitignored; local aggregates
```

---

## How evals work

Each case is a small incomplete Stelvio project plus an agent-facing prompt.

1. **Fixture** — starter `stlv_app.py` / handlers under `cases/<id>/fixture/`.
2. **Prompt** — `prompt.md` (what the agent sees; may deliberately omit API
   names such as SQS when the case tests idiom discovery).
3. **Checks** — `checks.toml` declares functional and semantic expectations
   (`meta.id` must match a registered grader).
4. **Grader** — `evals/graders/<eN>.py`, dispatched from
   `evals/graders/grade.py` by `meta.id`. Graders **construct the app and
   inspect components** (links, subscriptions, notifications, `customize`) and
   scan handler source. They **do not deploy** and do not require
   `stlv_resources.py` on disk.
5. **Known-good / known-bad** — under `solutions/`. Used only by unit tests in
   `tests/agent_support/`; those tests never call a live agent.

### Scoring families (keep separate)

| Family | Meaning |
|---|---|
| **Functional pass** | User-visible behavior is present (route, table, queue wire-up, etc.). Raw Pulumi / hand IAM can still pass functionally. |
| **Semantic checks** | Stelvio idioms (link, `Resources`, `subscribe`, `notify_*`, `customize`, minimal rename). Recorded per-check, not collapsed into one score. |
| **Observations** | Anti-patterns noted (manual IAM, hard-coded names, raw Pulumi, unrelated edits, …) — not automatic functional fails. |
| **Change footprint** | `files_changed`, `lines_added`, `lines_removed`, `unrelated_files_changed` — diagnostic, not a score. |
| **Failure tags** | e.g. `SCOPE`, `LINKING`, `EVENT_MODEL`, `HARNESS`, … when analyzing attempts. |

Result JSON shape: [`evals/schema/result.schema.json`](evals/schema/result.schema.json).

### Experimental conditions

| Condition | What the agent gets |
|---|---|
| `baseline` | Fixture + prompt only (no private skill). |
| `skill_invoked` | Same + skill copied under `.agents/skills/stelvio-best-practices/` + `AGENT_INSTRUCTION.txt` telling the agent to follow it. |
| `direct_context` | Same guidance inlined into the prompt / `DIRECT_CONTEXT.md` (diagnose skill *delivery* vs *content*). |

Primary comparison is **baseline vs skill_invoked** with one fixed agent/model
and identical budgets. Use `direct_context` only when A vs B is ambiguous.

### Grader unit tests (no agent)

```bash
uv run pytest tests/agent_support/ -q
```

---

## How to run evals on your machine

The sandbox is deliberately small: copy fixture → scrubbed env → your agent
command → grade → footprint → write JSON **outside** the temp workspace.

```bash
# from repo root; graders import as `graders.*`
export PYTHONPATH="agent-support/evals${PYTHONPATH:+:$PYTHONPATH}"

RESULTS="agent-support/evals/results/my-run"
mkdir -p "$RESULTS"

uv run python agent-support/evals/sandbox/run_local.py \
  --case e2-missing-link \
  --condition baseline \
  --attempt 1 \
  --timeout 900 \
  --results-dir "$RESULTS" \
  --agent codex \
  --model gpt-6-luna \
  --harness run_local+codex \
  --agent-command 'codex exec --skip-git-repo-check -s workspace-write -m gpt-6-luna "$(cat prompt.md)" </dev/null'
```

### Useful flags

| Flag | Role |
|---|---|
| `--case` | Directory name under `evals/cases/` (e.g. `e1-linked-dynamodb`). |
| `--condition` | `baseline` \| `skill_invoked` \| `direct_context`. |
| `--skill-dir` | Skill folder to mount for `skill_invoked` (typically `agent-support/marketplace/plugins/stelvio/skills/stelvio-best-practices`). |
| `--direct-context-file` | Optional guidance file for `direct_context`. |
| `--agent-command` | Shell string; cwd is the temp fixture copy; `prompt.md` is already there. |
| `--timeout` | Seconds (default `300`; agent runs often need more, e.g. `900`). |
| `--results-dir` | Must be outside the temp workspace; per-run file `{case}.{condition}.{attempt}.json`. |
| `--keep-workspace` | Leave the temp dir for debugging. |

### Environment scrubbing

The agent process gets an **allowlist** (`PATH`, `HOME`, `USER`, locale, …).
`AWS_*`, `PULUMI_*`, `STLV_*`, and `STELVIO_*` are stripped. Agent CLIs must
authenticate via files under `HOME` (or similar), not via those env prefixes.
No `stlv deploy` in the loop.

### Cases

```text
e1-linked-dynamodb
e2-missing-link
e3-async-queue
e4-s3-notify-function
e5-s3-notify-queue
e6-fanout-topic
e7-queue-customize-kms
e8-minimal-modification
```

Run several fresh attempts with identical settings; aggregate yourself (there is
no batch runner yet). Results under `evals/results/` are gitignored.

### Example baseline already on disk (gitignored)

Codex CLI, model `gpt-6-luna`, condition `baseline`, 3 attempts × 8 cases,
timeout 900s:

```bash
codex exec --skip-git-repo-check -s workspace-write -m gpt-6-luna "$(cat prompt.md)" </dev/null
```

Aggregate (when present locally):
`evals/results/baseline-codex-gpt-6-luna/aggregate.md`

| Headline | Value |
|---|---|
| Functional | **23/24** (95.8%) |
| Semantic-all | **20/24** (83.3%) |

Notable: e2 all three attempts functional-pass but `handler_unchanged=False`
(`SCOPE`×3); e5 attempt 2 grade load error `File path part should not contain
dots` (`HARNESS` + uncertain `STELVIO_MODEL`).

**Explicit-skill (`skill_invoked`) has not been run.** Phase D still recorded
`PROCEED` without A-vs-B numbers. Do not treat the baseline alone as proof that
the skill helps.

---

## Interpreting results

Per-run JSON includes at least: `task_id`, `condition`, `attempt`,
`functional_pass`, `semantic_checks`, plus optional observations, failure tags,
footprint, duration, `skill_loaded`, agent exit / timeout / error.

When reading an aggregate:

1. Report **functional** and **semantic-all** separately.
2. Look at per-case rates — one failing case can hide under a high headline.
3. Use failure tags and footprints diagnostically (e.g. “missing link → agent
   rewrote the handler” is a `SCOPE` / footprint story, not necessarily a
   functional miss).
4. Treat timeouts and “failed to load app” as harness / invalid-output issues;
   classify carefully.
5. Call this a **development-set** result, not a public benchmark.

---

## How to check if a skill was actually beneficial

Judge benefit by **comparing conditions**, same agent, model, attempt count,
and timeout:

1. Run **baseline** across e1–e8 (several attempts each).
2. Run **skill_invoked** with `--skill-dir` pointing at the skill and the same
   agent command/settings.
3. Compare functional pass rates, semantic check rates, failure-tag mix,
   footprints, and cost/duration if available.
4. If B barely moves the needle vs A, but you suspect the agent never loaded the
   skill, run **direct_context** on the ambiguous cases. Large C ≫ B suggests a
   delivery problem; C ≈ B ≈ A suggests weak guidance.

That A-vs-B comparison **has not been completed** for `stelvio-best-practices`
yet. Until it has, do not claim measured skill benefit from the baseline alone.

Even a positive A-vs-B on e1–e8 only shows the skill can influence these
development tasks — not that it generalizes.

---

## Repo split and installing skills

### Split

Only `agent-support/marketplace/` is exported. Workflow:
[`.github/workflows/split-agents.yml`](../.github/workflows/split-agents.yml).

- Triggers on push to `main` when `agent-support/marketplace/**` changes (or
  `workflow_dispatch`).
- `git-filter-repo` keeps `agent-support/marketplace/`, renames that prefix to
  repo root, pushes to **`stelviodev/agents`**.
- Evals never leave the main repo.

Status as of this writing:

- Private repo [stelviodev/agents](https://github.com/stelviodev/agents) exists
  and was seeded with the marketplace tree at root (commit
  `bcf42664dcf29422afe594532049e2c3671b09e3`).
- The split workflow file is in the **stelvio** repo; the **live split has not
  run** until that workflow (and marketplace) land on stelvio `main` and the
  workflow executes.

### Install paths (same skill files)

| Mechanism | How |
|---|---|
| **CLI** | `stlv agents install` / `update` (below) — skills from the installed wheel |
| **Claude** | `/plugin marketplace add stelviodev/agents` then `/plugin install stelvio@stelvio-agents` |
| **Cursor** | Load via `.cursor-plugin/marketplace.json` |
| **Codex** | `.agents/plugins/marketplace.json` in the agents repo |
| **`npx skills`** | `npx skills add stelviodev/agents` (or `…/plugins/stelvio` if depth misses) |
| **OpenCode** | No marketplace manifest; use CLI project install → `.agents/skills/` |

User-oriented install copy also lives in
[`marketplace/README.md`](marketplace/README.md) (what end users see after the
split).

### Wheel packaging

Hatch packs skills into the wheel:

```toml
[tool.hatch.build.targets.wheel.force-include]
"agent-support/marketplace/plugins/stelvio/skills" = "stelvio/agent_support/skills"
```

The sdist also includes that skills path so wheels built from sdist still ship
them. `stlv agents install` reads packaged skills (with a source-checkout
fallback during development).

---

## CLI: install and update skills

```bash
stlv agents install --target cursor,claude
stlv agents install --target cursor --target codex
stlv agents update --target cursor,claude
stlv agents update --target cursor,claude --force
stlv agents install --target cursor,codex,claude,opencode --global
```

| Detail | Behavior |
|---|---|
| `--target` | **Required.** Comma-separated and/or repeatable. Values: `cursor`, `codex`, `claude`, `opencode`. |
| Project (default) | `cursor` / `codex` / `opencode` → write **once** to `.agents/skills/<name>/`. `claude` → `.claude/skills/<name>/`. |
| `--global` | Same layout under `$HOME` (`.agents/skills/`, `.claude/skills/`). |
| Manifest | Project: `.stelvio/agents.json`. Global: `~/.stelvio/agents.json`. Stores version, targets, skill names, content hashes. |
| `update` | Refuses if owned files’ hashes drifted, unless `--force`. Does not fetch GitHub — upgrade the `stelvio` package, then `update`. |
| Copy | Files are copied; no symlinks. |

Implementation: `stelvio/cli/agents_command.py`. Tests:
`tests/test_agents_command.py`.

---

## How to add a skill

1. Create
   `marketplace/plugins/stelvio/skills/<skill-name>/SKILL.md`
   (front matter `name` / `description`; keep prose **user-facing** — no eval
   ids, no “Maps to”, no internal contract nicknames).
2. Optional `references/` under that skill only if maintainers decide the skill
   needs them; do not put eval fixtures there.
3. Confirm marketplace / plugin manifests still point at `plugins/stelvio`
   (they already scan `skills/`).
4. Wheel packaging already force-includes the whole `skills/` tree — no
   pyproject change unless the source path moves.
5. Exercise with `stlv agents install --target cursor` from a source checkout
   or editable install.
6. If you want measured benefit: write/extend evals first when possible, run
   baseline, then skill_invoked, then decide whether to revise the skill.
7. After merge to stelvio `main`, the split workflow (once live) publishes
   marketplace changes to `stelviodev/agents`. Tag that repo when cutting a
   Stelvio release so marketplace users can pin `#vX.Y.Z`.

---

## How to add an eval

1. **Contract** — Decide functional vs semantic checks and failure modes
   (extend `notes/stelvio-best-practices-v0.md` or a follow-on note if this is
   a new skill). Prefer cases that do not spoil the skill’s teaching by naming
   the target API when discovery is the point.
2. **Case directory** — `evals/cases/<case-id>/` with:
   - `prompt.md`
   - `fixture/` (minimal installable Stelvio app)
   - `checks.toml` with `[meta] id = "<case-id>"` plus functional/semantic
     (and optional `[footprint]`) sections
   - `solutions/good/` and at least one `solutions/bad-…/` for grader tests
3. **Grader** — Add `evals/graders/<slug>.py` and register it in
   `evals/graders/grade.py` `_GRADERS`.
4. **Unit tests** — Extend `tests/agent_support/` so good passes and bad fails
   as expected (`uv run pytest tests/agent_support/ -q`).
5. **Smoke with sandbox** — Run `run_local.py` once with a known-good stand-in
   or a real agent command; confirm result JSON lands under your `--results-dir`.
6. **Do not** leak case ids or “Maps to” into user-facing `SKILL.md`.

---

## Related files

| Path | Role |
|---|---|
| `notes/stelvio-user-agent-plan-v2.md` | Experimental phases and productization status |
| `notes/stelvio-best-practices-v0.md` | First skill’s behavioral contract |
| `.github/workflows/split-agents.yml` | Marketplace → `stelviodev/agents` |
| `stelvio/cli/agents_command.py` | `stlv agents` |
| `stelvio/agent_support/` | Package marker for bundled skills |
| `tests/agent_support/` | Grader + sandbox unit tests |
| `tests/test_agents_command.py` | CLI install/update tests |
