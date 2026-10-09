# Custom templates

A template supplies the files for a new Stelvio application. Stelvio copies it once;
changes to the template do not update applications already created from it.

## Initialize from a template

Run the command in the directory where the new application should live:

```bash
mkdir orders-service
cd orders-service
uvx --from stelvio stlv init --template gh:YOUR_ORG/app-templates@v1.0.0/http-service
```

If the CLI is already installed, use `stlv init --template` with the same selector.
Replace the organization, repository, tag, and subdirectory with your template's values.

### Template selectors

| Selector | Source |
|----------|--------|
| `base` | The `base` directory in `stelviodev/templates`, on `main`. |
| `gh:YOUR_ORG/http-service-template` | The repository root on its default branch. |
| `gh:YOUR_ORG/app-templates/http-service` | The `http-service` directory on the repository's default branch. |
| `gh:YOUR_ORG/http-service-template@v1.0.0` | The repository root at tag `v1.0.0`. |
| `gh:YOUR_ORG/app-templates@v1.0.0/http-service` | The `http-service` directory at tag `v1.0.0`. |

Omit `@` to use the repository's default branch, whether that is `main`, `master`, or
another name. Put a branch or tag after `@` to select that ref. Named branches and tags
work because Stelvio passes them to
[git clone --branch](https://git-scm.com/docs/git-clone#Documentation/git-clone.txt---branchltnamegt).
An arbitrary commit SHA is not a valid selector. Use a release tag for a repeatable
starting point and keep published tags unchanged.

Use branch and tag names without `/`. The selector treats the first slash after `@` as the
start of the subdirectory. Nested subdirectories such as `python/http-service` are
supported. The `gh:` form targets GitHub.com. Local filesystem paths, full repository
URLs, and other Git hosts are not template selectors.

## What gets copied

Stelvio copies the contents of the selected directory, including dotfiles and nested
folders, into the current directory. Files outside that directory are not copied.
The template's Git history is removed, and initialization offers to create a new Git
repository when appropriate.

Files are copied as they are. Stelvio does not replace placeholders, rename the
application, install dependencies, or run setup hooks. The application keeps the name
written in the template's `stlv_app.py`.

## Template layout

A template needs no manifest or special template engine. The selected directory must
contain a `stlv_app.py` file directly, so it lands at the new application's root.
For example:

```text
app-templates/
├── README.md
└── http-service/
    ├── README.md
    ├── pyproject.toml
    ├── .gitignore
    ├── stlv_app.py
    └── functions/
        ├── __init__.py
        └── health.py
```

Selecting `http-service` copies its contents, including its README, but leaves out the
repository-level README.

## When initialization stops

The command exits with a non-zero status when Git is missing, cloning fails, the selected
branch, tag, or subdirectory does not exist, or the selected directory has no `stlv_app.py`.
Git failures include Git's own error message.

If a template file or directory conflicts with an existing path, including a broken
symlink, the command stops before copying. If copying fails partway through, Stelvio
removes the paths it created and keeps existing files. These failures leave the destination
unchanged.

If `stlv_app.py` already exists, initialization prints "Stelvio project already exists"
and exits successfully without applying the template.

## Private repositories

Private repositories use the same selector. Stelvio invokes Git with an HTTPS GitHub URL,
so Git needs credentials that can read the repository over HTTPS. Configure those
credentials through your Git credential helper before running initialization.
Do not put a token in the selector or template files.
