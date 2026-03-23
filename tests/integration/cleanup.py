"""Clean up orphaned AWS resources from interrupted integration test runs.

When a test process is killed (SIGKILL, crash, Ctrl+C during teardown),
the stelvio_env fixture's destroy() never runs, leaving:
  1. Temp directories (stelvio-test-*) with Pulumi state
  2. Real AWS resources still provisioned

Three levels of cleanup:
  Level 1 (default): Pulumi state files in /tmp. Works when state dirs survive.
  Level 2 (--tags): AWS Resource Groups Tagging API. Finds resources tagged
      stelvio:env=test with stelvio:app matching "stelvio-<6hex>".
  Level 3 (--names): Per-service name-prefix scan. Finds resources named
      stelvio-<hex>-test-*.

Usage:
    # Level 1 only (default)
    STELVIO_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py
    STELVIO_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py --dry-run

    # Level 2: tag-based scan
    STELVIO_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py --tags --dry-run

    # Level 3: name-prefix scan
    STELVIO_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py --names --dry-run

    # Both levels combined, actually delete
    STELVIO_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py --tags --names

    # Cross-region (e.g. eu-west-1 app + us-east-1 ACM certs)
    STELVIO_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py \
        --tags --region us-east-1 --region eu-west-1

    # CI — run as a step AFTER tests, in the same job
    uv run python tests/integration/cleanup.py --tags --names
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    select_stack,
)
from semver import VersionInfo

# Add project root to path so we can import stelvio
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from stelvio.pulumi import get_stelvio_config_dir

STATE_DIR_PREFIX = "stelvio-test-"
PASSPHRASE = "test-passphrase-not-secret"  # noqa: S105


def _get_pulumi_command() -> PulumiCommand:
    # Same logic as stelvio_test_env.py — duplicated because cleanup runs standalone
    return PulumiCommand.install(
        root=str(get_stelvio_config_dir()),
        version=VersionInfo.parse(version("pulumi")),
    )


def _find_orphaned_dirs() -> list[Path]:
    """Find all stelvio-test-* directories in the system temp dir."""
    tmp = Path(tempfile.gettempdir())
    return sorted(tmp.glob(f"{STATE_DIR_PREFIX}*"))


def _parse_state(workdir: Path) -> dict | None:
    """Parse Pulumi state from a workdir. Returns None if no state found."""
    stacks_dir = workdir / ".pulumi" / "stacks"
    if not stacks_dir.exists():
        return None

    for state_file in stacks_dir.rglob("*.json"):
        if state_file.name.endswith((".bak", ".attrs")):
            continue
        try:
            data = json.loads(state_file.read_text())
            deployment = data.get("checkpoint", {}).get("latest", {})
            resources = deployment.get("resources", [])
            real_resources = [r for r in resources if not r["type"].startswith("pulumi:")]
            # Pulumi local backend: .pulumi/stacks/{project_name}/{stack_name}.json
            return {
                "project_name": state_file.parent.name,
                "stack_name": state_file.stem,
                "resource_count": len(real_resources),
                "resource_types": [r["type"] for r in real_resources],
            }
        except (json.JSONDecodeError, KeyError):
            continue

    return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    else:
        return True


def _remove_stale_locks(workdir: Path) -> None:
    """Remove stale Pulumi lock files left by killed processes."""
    locks_dir = workdir / ".pulumi" / "locks"
    if not locks_dir.exists():
        return
    for lock_file in locks_dir.rglob("*.json"):
        try:
            lock_data = json.loads(lock_file.read_text())
            pid = lock_data.get("pid")
            if pid and _is_pid_alive(pid):
                print(f"  Skipping lock {lock_file.name} — process {pid} is still alive")
                continue
        except (json.JSONDecodeError, KeyError, OSError):
            pass
        lock_file.unlink()
        print(f"  Removed stale lock: {lock_file.name}")


def _destroy_stack(
    workdir: Path,
    project_name: str,
    stack_name: str,
    pulumi_cmd: PulumiCommand,
) -> bool:
    """Destroy a stack's resources. Returns True on success."""
    _remove_stale_locks(workdir)

    aws_profile = os.environ.get("STELVIO_TEST_AWS_PROFILE")
    aws_region = os.environ.get("STELVIO_TEST_AWS_REGION", "us-east-1")

    env_vars = {"PULUMI_CONFIG_PASSPHRASE": PASSPHRASE}
    if aws_region:
        env_vars["AWS_REGION"] = aws_region
    if aws_profile:
        env_vars["AWS_PROFILE"] = aws_profile

    backend = ProjectBackend(f"file://{workdir}")
    project_settings = ProjectSettings(name=project_name, runtime="python", backend=backend)

    opts = LocalWorkspaceOptions(
        pulumi_command=pulumi_cmd,
        env_vars=env_vars,
        project_settings=project_settings,
        pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
    )

    def _noop():
        pass

    stack = select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=_noop,
        opts=opts,
    )

    # Refresh first to reconcile pending operations (e.g. interrupted CREATEs)
    # against actual AWS state. Without this, destroy silently skips resources
    # that were created in AWS but recorded as "pending CREATE" in Pulumi state.
    try:
        stack.refresh(on_output=print)
    except Exception as e:
        print(f"  Refresh failed (continuing with destroy): {e}")

    try:
        stack.destroy(on_output=print)
    except Exception as e:
        print(f"  Destroy failed: {e}")
        print("  Refreshing state and retrying destroy...")
        try:
            stack.refresh(on_output=print)
            stack.destroy(on_output=print)
        except Exception as e2:
            print(f"  Retry after refresh also failed: {e2}")
            return False

    return True


def _scan_dirs(dirs: list[Path]) -> tuple[list[Path], list[tuple[Path, dict]]]:
    """Classify orphaned dirs into empty and has-resources."""
    empty = []
    has_resources = []

    for d in dirs:
        state = _parse_state(d)
        if state is None or state["resource_count"] == 0:
            empty.append(d)
            status = "empty" if state is None else "no resources"
            print(f"  {d.name}: {status}")
        else:
            has_resources.append((d, state))
            types = ", ".join(t.split(":")[-1] for t in state["resource_types"])
            print(
                f"  {d.name}: {state['resource_count']} resource(s) "
                f"[{types}] (project={state['project_name']} stack={state['stack_name']})"
            )

    return empty, has_resources


def _destroy_all(has_resources: list[tuple[Path, dict]]) -> None:
    """Destroy all stacks with resources."""
    print(f"\nDestroying {len(has_resources)} stack(s) with resources...")
    pulumi_cmd = _get_pulumi_command()
    succeeded = 0
    failed = 0

    for d, state in has_resources:
        print(f"\n--- {state['project_name']}/{state['stack_name']} ---")
        if _destroy_stack(d, state["project_name"], state["stack_name"], pulumi_cmd):
            shutil.rmtree(d, ignore_errors=True)
            print(f"  Destroyed and cleaned up {d.name}")
            succeeded += 1
        else:
            print(f"  FAILED — keeping {d} for manual cleanup")
            failed += 1

    print(f"\nDone: {succeeded} destroyed, {failed} failed")


def _run_level1(dry_run: bool) -> None:
    """Level 1: Pulumi state file cleanup."""
    dirs = _find_orphaned_dirs()
    if not dirs:
        print("No orphaned test directories found.")
        return

    print(f"Found {len(dirs)} orphaned test director{'y' if len(dirs) == 1 else 'ies'}:\n")

    empty, has_resources = _scan_dirs(dirs)

    if dry_run:
        print(f"\nDry run: {len(has_resources)} with resources, {len(empty)} empty")
        return

    if empty:
        print(f"\nRemoving {len(empty)} empty director{'y' if len(empty) == 1 else 'ies'}...")
        for d in empty:
            shutil.rmtree(d, ignore_errors=True)
            print(f"  Removed {d.name}")

    if has_resources:
        _destroy_all(has_resources)
    else:
        print("\nNo stacks with resources to destroy.")


def _run_aws_cleanup(*, tags: bool, names: bool, dry_run: bool, regions: list[str]) -> None:
    """Level 2/3: AWS resource discovery and deletion."""
    from cleanup_aws import (
        deduplicate,
        delete_resources,
        discover_by_name,
        discover_by_tags,
    )

    profile = os.environ.get("STELVIO_TEST_AWS_PROFILE")
    all_resources = []

    if tags:
        print(f"\n=== Level 2: Tag-based scan (regions: {', '.join(regions)}) ===")
        found = discover_by_tags(profile, regions)
        print(f"Found {len(found)} resource(s) by tags")
        all_resources.extend(found)

    if names:
        print(f"\n=== Level 3: Name-prefix scan (regions: {', '.join(regions)}) ===")
        found = discover_by_name(profile, regions)
        print(f"Found {len(found)} resource(s) by name")
        all_resources.extend(found)

    resources = deduplicate(all_resources)
    print(f"\n{len(resources)} unique resource(s) after deduplication")

    if not resources:
        print("Nothing to clean up.")
        return

    # Print summary grouped by service
    by_service: dict[str, list] = {}
    for r in resources:
        by_service.setdefault(r.service, []).append(r)
    for service, items in sorted(by_service.items()):
        print(f"  {service}: {len(items)}")
        for item in items:
            print(f"    {item.name} ({item.region})")

    if dry_run:
        print("\nDry run — no resources deleted.")
        return

    print("\nDeleting resources...")
    succeeded, failed = delete_resources(profile, resources)
    print(f"\nDone: {succeeded} deleted, {failed} failed")


def main():
    parser = argparse.ArgumentParser(description="Clean up orphaned integration test resources")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List orphaned resources without destroying them",
    )
    parser.add_argument(
        "--tags",
        action="store_true",
        help="Level 2: find resources by stelvio:env=test tag",
    )
    parser.add_argument(
        "--names",
        action="store_true",
        help="Level 3: find resources by stelvio-<hex>-test- name prefix",
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="AWS region(s) to scan (repeatable, default: us-east-1)",
    )
    args = parser.parse_args()

    aws_cleanup = args.tags or args.names
    profile = os.environ.get("STELVIO_TEST_AWS_PROFILE")

    if not args.dry_run and not profile:
        print("Error: STELVIO_TEST_AWS_PROFILE env var required (or use --dry-run)")
        sys.exit(1)

    regions = args.regions or [os.environ.get("STELVIO_TEST_AWS_REGION", "us-east-1")]

    if not aws_cleanup:
        # Default: level 1 only
        _run_level1(args.dry_run)
    else:
        _run_aws_cleanup(tags=args.tags, names=args.names, dry_run=args.dry_run, regions=regions)


if __name__ == "__main__":
    main()
