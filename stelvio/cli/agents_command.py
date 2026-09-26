"""Install and update Stelvio agent skills for supported coding agents."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from stelvio.exceptions import StelvioValidationError

logger = logging.getLogger(__name__)
console = Console()

SUPPORTED_TARGETS = frozenset({"cursor", "codex", "claude", "opencode"})
SHARED_AGENTS_TARGETS = frozenset({"cursor", "codex", "opencode"})
MANIFEST_FILENAME = "agents.json"
SKILL_MARKER = "SKILL.md"

# Overridable in tests.
_skills_root_override: Path | None = None


def _stelvio_version() -> str:
    return metadata.version("stelvio")


def get_skills_root() -> Path:
    """Return the skills tree to install from.

    Prefer packaged skills under ``stelvio.agent_support.skills`` (wheel
    ``force-include``). Fall back to the repo path used during development.
    """
    if _skills_root_override is not None:
        return _skills_root_override

    try:
        packaged = resources.files("stelvio.agent_support").joinpath("skills")
        if packaged.is_dir():
            # Real filesystem path when installed / force-included beside the package.
            packaged_path = Path(str(packaged))
            if packaged_path.is_dir() and any(packaged_path.iterdir()):
                return packaged_path
    except (ModuleNotFoundError, TypeError, ValueError, OSError):
        logger.debug("Packaged agent skills not available", exc_info=True)

    # stelvio/cli/agents_command.py → repo root is parents[2] in a source checkout.
    repo_skills = (
        Path(__file__).resolve().parents[2]
        / "agent-support"
        / "marketplace"
        / "plugins"
        / "stelvio"
        / "skills"
    )
    if repo_skills.is_dir() and any(repo_skills.iterdir()):
        return repo_skills

    raise StelvioValidationError(
        "No Stelvio agent skills found. Reinstall the stelvio package or run from a "
        "source checkout that includes agent-support/marketplace/plugins/stelvio/skills."
    )


def _parse_targets(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]
) -> list[str]:
    if not value:
        raise click.BadParameter(
            "Required. Pass one or more of: cursor, codex, claude, opencode "
            "(comma-separated and/or repeatable)."
        )
    parsed: list[str] = []
    seen: set[str] = set()
    for item in value:
        for part in item.split(","):
            target = part.strip().lower()
            if not target:
                continue
            if target not in SUPPORTED_TARGETS:
                raise click.BadParameter(
                    f"Unknown target '{target}'. "
                    f"Supported: {', '.join(sorted(SUPPORTED_TARGETS))}."
                )
            if target not in seen:
                seen.add(target)
                parsed.append(target)
    if not parsed:
        raise click.BadParameter("At least one target is required.")
    return parsed


def _destination_kinds(targets: list[str]) -> list[str]:
    """Map targets to unique skill destination kinds: 'agents' and/or 'claude'."""
    kinds: list[str] = []
    if SHARED_AGENTS_TARGETS.intersection(targets):
        kinds.append("agents")
    if "claude" in targets:
        kinds.append("claude")
    return kinds


def _skills_dir_for_kind(kind: str, *, base: Path) -> Path:
    if kind == "agents":
        return base / ".agents" / "skills"
    if kind == "claude":
        return base / ".claude" / "skills"
    raise ValueError(f"Unknown destination kind: {kind}")


def _manifest_path(*, global_scope: bool, base: Path) -> Path:
    if global_scope:
        return base / ".stelvio" / MANIFEST_FILENAME
    return base / ".stelvio" / MANIFEST_FILENAME


def _install_base(*, global_scope: bool, home: Path, cwd: Path) -> Path:
    return home if global_scope else cwd


def _list_skill_names(skills_root: Path) -> list[str]:
    names = [
        child.name
        for child in sorted(skills_root.iterdir())
        if child.is_dir() and (child / SKILL_MARKER).is_file()
    ]
    if not names:
        raise StelvioValidationError(f"No skills with {SKILL_MARKER} found under {skills_root}")
    return names


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel_skill_file(kind: str, skill_name: str, relative: Path) -> str:
    prefix = ".agents/skills" if kind == "agents" else ".claude/skills"
    return f"{prefix}/{skill_name}/{relative.as_posix()}"


def _iter_skill_files(skill_dir: Path) -> list[Path]:
    return sorted(path for path in skill_dir.rglob("*") if path.is_file())


def _copy_skill(src_skill: Path, dest_skill: Path) -> dict[str, str]:
    """Copy one skill directory (files only; no symlinks). Return relative→hash map."""
    if dest_skill.exists():
        shutil.rmtree(dest_skill)
    dest_skill.mkdir(parents=True, exist_ok=True)

    hashes: dict[str, str] = {}
    for src_file in _iter_skill_files(src_skill):
        rel = src_file.relative_to(src_skill)
        dest_file = dest_skill / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        hashes[rel.as_posix()] = _file_sha256(dest_file)
    return hashes


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StelvioValidationError(f"Invalid agents manifest at {path}: {e}") from e
    if not isinstance(data, dict):
        raise StelvioValidationError(f"Invalid agents manifest at {path}: expected a JSON object")
    return data


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _find_dirty_files(
    *,
    base: Path,
    manifest: dict[str, Any],
    kinds: list[str],
) -> list[str]:
    """Return manifest-owned paths under selected destinations whose hashes no longer match."""
    files: dict[str, str] = manifest.get("files") or {}
    dirty: list[str] = []
    kind_prefixes = {
        "agents": ".agents/skills/",
        "claude": ".claude/skills/",
    }
    selected_prefixes = [kind_prefixes[k] for k in kinds]
    for rel_path, expected_hash in files.items():
        if not any(rel_path.startswith(prefix) for prefix in selected_prefixes):
            continue
        absolute = base / rel_path
        if not absolute.is_file():
            dirty.append(rel_path)
            continue
        if _file_sha256(absolute) != expected_hash:
            dirty.append(rel_path)
    return dirty


def _merge_targets(existing: list[str] | None, new: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for target in [*(existing or []), *new]:
        if target not in seen:
            seen.add(target)
            merged.append(target)
    return merged


def _install_or_update(  # noqa: PLR0913
    *,
    targets: list[str],
    global_scope: bool,
    force: bool,
    update: bool,
    cwd: Path | None = None,
    home: Path | None = None,
    skills_root: Path | None = None,
) -> None:
    cwd = cwd or Path.cwd()
    home = home or Path.home()
    base = _install_base(global_scope=global_scope, home=home, cwd=cwd)
    manifest_path = _manifest_path(global_scope=global_scope, base=base)
    root = skills_root or get_skills_root()
    skill_names = _list_skill_names(root)
    kinds = _destination_kinds(targets)

    existing = _load_manifest(manifest_path)
    if update and existing is None:
        raise StelvioValidationError(
            f"No agents manifest at {manifest_path}. Run 'stlv agents install' first."
        )

    if update and existing is not None and not force:
        dirty = _find_dirty_files(base=base, manifest=existing, kinds=kinds)
        if dirty:
            listed = "\n".join(f"  - {path}" for path in dirty)
            raise StelvioValidationError(
                "Refusing to update: installed skill files were modified since install.\n"
                f"{listed}\n"
                "Re-run with --force to overwrite, or restore the files first."
            )

    files: dict[str, str] = dict(existing.get("files", {})) if existing else {}
    # Drop hashes for destinations we are about to rewrite so removed files do not linger.
    for kind in kinds:
        prefix = ".agents/skills/" if kind == "agents" else ".claude/skills/"
        files = {path: digest for path, digest in files.items() if not path.startswith(prefix)}

    for kind in kinds:
        dest_root = _skills_dir_for_kind(kind, base=base)
        dest_root.mkdir(parents=True, exist_ok=True)
        for skill_name in skill_names:
            src = root / skill_name
            dest = dest_root / skill_name
            skill_hashes = _copy_skill(src, dest)
            for rel, digest in skill_hashes.items():
                files[_rel_skill_file(kind, skill_name, Path(rel))] = digest
            console.print(
                f"[bold green]✓[/bold green] "
                f"{'Updated' if update else 'Installed'} {skill_name} → {dest}"
            )

    manifest = {
        "version": _stelvio_version(),
        "targets": _merge_targets(existing.get("targets") if existing else None, targets),
        "skills": skill_names,
        "files": dict(sorted(files.items())),
        "scope": "global" if global_scope else "project",
    }
    _write_manifest(manifest_path, manifest)
    console.print(f"[bold green]✓[/bold green] Wrote manifest {manifest_path}")


@click.group()
def agents() -> None:
    """Install and update Stelvio skills for coding agents."""


@agents.command("install")
@click.option(
    "--target",
    "targets",
    multiple=True,
    required=True,
    callback=_parse_targets,
    help="Agent target(s): cursor, codex, claude, opencode. Comma-separated and/or repeatable.",
)
@click.option(
    "--global",
    "global_scope",
    is_flag=True,
    help="Install into the user-global skills directories instead of the project.",
)
def install_cmd(targets: list[str], global_scope: bool) -> None:
    """Copy bundled Stelvio skills into agent skill directories."""
    try:
        _install_or_update(targets=targets, global_scope=global_scope, force=True, update=False)
    except StelvioValidationError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(2) from None


@agents.command("update")
@click.option(
    "--target",
    "targets",
    multiple=True,
    required=True,
    callback=_parse_targets,
    help="Agent target(s): cursor, codex, claude, opencode. Comma-separated and/or repeatable.",
)
@click.option(
    "--global",
    "global_scope",
    is_flag=True,
    help="Update skills in the user-global directories instead of the project.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite skill files even when their content hashes no longer match the manifest.",
)
def update_cmd(targets: list[str], global_scope: bool, force: bool) -> None:
    """Refresh installed skills from the installed Stelvio package."""
    try:
        _install_or_update(targets=targets, global_scope=global_scope, force=force, update=True)
    except StelvioValidationError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(2) from None
