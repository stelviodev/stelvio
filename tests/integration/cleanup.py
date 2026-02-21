"""Clean up orphaned AWS resources from interrupted integration test runs.

When a test process is killed (SIGKILL, crash, Ctrl+C during teardown),
the stelvio_env fixture's destroy() never runs, leaving:
  1. Temp directories (stelvio-test-*) with Pulumi state
  2. Real AWS resources still provisioned

This script finds those orphaned state directories, destroys the AWS
resources via Pulumi, and removes the temp directories.

This is "level 1" cleanup — it relies on Pulumi state files in the local
temp directory. It works locally and in CI when run as a post-test step
in the same job (the temp dir is still available). For resources orphaned
by a killed CI runner, a future "level 2" (tag-based) or "level 3"
(naming-prefix-based) scanner would be needed.

Usage (local):
    STLV_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py
    STLV_TEST_AWS_PROFILE=michal uv run python tests/integration/cleanup.py --dry-run

Usage (CI — run as a step AFTER tests, in the same job):
    - name: Clean up orphaned test resources
      if: always()
      run: uv run python tests/integration/cleanup.py
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

    aws_profile = os.environ.get("STLV_TEST_AWS_PROFILE")
    aws_region = os.environ.get("STLV_TEST_AWS_REGION", "us-east-1")

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

    try:
        stack.destroy(on_output=print)
    except Exception as e:
        print(f"  Destroy failed: {e}")
        print("  Refreshing state and retrying...")
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


def main():
    parser = argparse.ArgumentParser(description="Clean up orphaned integration test resources")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List orphaned resources without destroying them",
    )
    args = parser.parse_args()

    if not args.dry_run and not os.environ.get("STLV_TEST_AWS_PROFILE"):
        print("Error: STLV_TEST_AWS_PROFILE env var required (or use --dry-run)")
        sys.exit(1)

    dirs = _find_orphaned_dirs()
    if not dirs:
        print("No orphaned test directories found.")
        return

    print(f"Found {len(dirs)} orphaned test director{'y' if len(dirs) == 1 else 'ies'}:\n")

    empty, has_resources = _scan_dirs(dirs)

    if args.dry_run:
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


if __name__ == "__main__":
    main()
