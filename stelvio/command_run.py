"""Command execution context and state management for Stelvio CLI operations.

This module provides CommandRun, a context manager that handles the full lifecycle
of CLI operations: loading the app, managing state (pull/push to S3), locking,
and partial push for crash recovery.

Command Behavior Summary:
    +--------------+------+------------+---------------------+
    | Command      | Lock | Push State | Snapshot            |
    +--------------+------+------------+---------------------+
    | diff         | NO   | NO         | NO                  |
    | deploy       | YES  | YES        | CREATE              |
    | refresh      | YES  | YES        | NO                  |
    | destroy      | YES  | YES        | DELETE (if empty)   |
    | outputs      | NO   | NO         | NO                  |
    | unlock       | N/A  | NO         | NO                  |
    | state list   | NO   | NO         | NO                  |
    | state rm     | YES  | YES        | NO                  |
    | state repair | YES  | YES        | NO                  |
    +--------------+------+------------+---------------------+

Partial Push Architecture:
    During deploy/destroy/refresh, state is continuously pushed to S3 to prevent
    data loss on crash. Uses event-driven + timer fallback approach:

    Main Thread                    Background Thread
    -----------                    -----------------
    start_partial_push() ────────► _partial_push_loop()
                                        │
    stack.up/destroy/refresh           wait(trigger OR 5s)
         │                              │
    resource completes ──────────► trigger_push()
         │                              │
    stop_partial_push() ─────────► exit loop
         │
    push_state() ◄─── final guaranteed push

    Safety: JSON validation, temp file copy (race fix), hash deduplication.
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import threading
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable

    from pulumi.automation import EngineEvent

    from stelvio.rich_deployment_handler import RichDeploymentHandler

from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    Stack,
    create_or_select_stack,
    fully_qualified_stack_name,
)
from semver import VersionInfo

from stelvio.app import StelvioApp
from stelvio.aws.home import AwsHome
from stelvio.context import AppContext, _ContextStore, context
from stelvio.exceptions import StateLockedError, StelvioProjectError
from stelvio.home import Home
from stelvio.project import get_dot_stelvio_dir, get_project_root, get_user_env
from stelvio.pulumi import get_stelvio_config_dir

logger = logging.getLogger(__name__)

BOOTSTRAP_PARAM = "/stlv/bootstrap"
PASSPHRASE_PARAM = "/stlv/passphrase/{app}/{env}"  # noqa: S105
STATE_KEY = "state/{app}/{env}.json"
LOCK_KEY = "lock/{app}/{env}.json"
SNAPSHOT_KEY = "snapshot/{app}/{env}/{update_id}.json"
UPDATE_KEY = "update/{app}/{env}/{update_id}.json"

# TODO: Event Log Storage (not implemented)
#
# We may want to store Pulumi events to S3 for debugging failed deploys.
# Pulumi Automation API doesn't expose the event log file - it creates a temp
# file internally and deletes it after operations.
#
# Options if we revisit:
# 1. Serialize on_event callback: Write each EngineEvent to file using recursive
#    __dict__ serialization. Upload to eventlog/{app}/{env}/{update_id}.json
# 2. Switch to Pulumi CLI instead of Automation API: Run e.g. `pulumi up --event-log <path>`
#    via subprocess and tail event log file for structured events.

CURRENT_BOOTSTRAP_VERSION = 2


def _generate_update_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    random_suffix = secrets.token_hex(4)  # 8 hex chars
    return f"{timestamp}-{random_suffix}"


def _init_storage(home: Home) -> str:
    bootstrap_str = home.read_param(BOOTSTRAP_PARAM)
    bootstrap = json.loads(bootstrap_str) if bootstrap_str else None
    if bootstrap and bootstrap["version"] == CURRENT_BOOTSTRAP_VERSION:
        return home.init_storage(bootstrap["state"])

    # No or old bootstrap
    storage_name = home.init_storage()
    bootstrap = {"version": CURRENT_BOOTSTRAP_VERSION, "state": storage_name}
    home.write_param(BOOTSTRAP_PARAM, json.dumps(bootstrap))
    return storage_name


def _get_or_create_passphrase(home: Home, app: str, env: str) -> str:
    param_name = PASSPHRASE_PARAM.format(app=app, env=env)
    passphrase = home.read_param(param_name)
    if not passphrase:
        passphrase = secrets.token_urlsafe(32)
        home.write_param(
            param_name,
            passphrase,
            "DO NOT DELETE! YOU WILL NOT BE ABLE TO RECOVER STATE OF ENVIRONMENT!",
            secure=True,
        )
    return passphrase


def _setup_app_home_storage(env: str, dev_mode: bool = False) -> tuple[Home, AppContext]:
    """Load app and initialize home storage."""
    _load_stlv_app(env, dev_mode)
    ctx = context()
    if ctx.home == "aws":
        home: Home = AwsHome(ctx.aws.profile, ctx.aws.region)
    else:
        raise ValueError(f"Unknown home type: {ctx.home}")
    _init_storage(home)
    return home, ctx


def force_unlock(env: str) -> dict | None:
    """Force unlock state. Returns lock info if lock existed, None otherwise."""
    home, ctx = _setup_app_home_storage(env)
    app_name = ctx.name
    lock_key = LOCK_KEY.format(app=app_name, env=env)
    if not home.file_exists(lock_key):
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        lock_path = tmpdir_path / "lock.json"
        home.read_file(lock_key, lock_path)
        lock_info = json.loads(lock_path.read_text())

        # Mark update as failed
        update_key = UPDATE_KEY.format(app=app_name, env=env, update_id=lock_info["update_id"])
        update_path = tmpdir_path / "update.json"
        if home.read_file(update_key, update_path):
            update_info = json.loads(update_path.read_text())
            update_info["time_completed"] = datetime.now(UTC).isoformat()
            update_info["errors"] = ["Force unlocked - operation did not complete"]
            update_path.write_text(json.dumps(update_info))
            home.write_file(update_key, update_path)

        # Delete lock
        home.delete_file(lock_key)

    return lock_info


def _load_stlv_app(env: str, dev_mode: bool) -> None:
    logger.debug("CWD %s", Path.cwd())
    logger.debug("SYS PATH %s", sys.path)

    original_sys_path = list(sys.path)
    try:
        project_root = get_project_root()
    except ValueError as e:
        logger.exception("Failed to find Stelvio project")
        raise StelvioProjectError(
            "No Stelvio project found. Run 'stlv init' to create a new project in this directory."
        ) from e

    logger.debug("PROJECT ROOT: %s", project_root)
    if project_root not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        import_module("stlv_app")
    finally:
        sys.path = original_sys_path

    app = StelvioApp.get_instance()
    logger.debug("Getting project configuration for environment: %s", env)
    config = app._execute_user_config_func(env)  # noqa: SLF001
    project_name = app._name  # noqa: SLF001

    _ContextStore.set(
        AppContext(
            name=project_name,
            env=env,
            aws=config.aws,
            dns=config.dns,
            home=config.home,
            dev_mode=dev_mode,
        )
    )
    # Validate environment
    username = get_user_env()
    if not config.is_valid_environment(env, username):
        raise ValueError(
            f"Invalid environment '{env}'. Use your username '{username}' for personal "
            f"environments or one of: {config.environments}"
        )


def _create_stack(ctx: AppContext, passphrase: str, workdir: Path) -> Stack:
    stack_name = fully_qualified_stack_name("organization", ctx.name, ctx.env)
    logger.debug("Fully qualified stack name: %s", stack_name)
    backend = ProjectBackend(f"file://{workdir}")
    project_settings = ProjectSettings(name=ctx.name, runtime="python", backend=backend)
    logger.debug("Setting up workspace")
    env_vars = {"PULUMI_CONFIG_PASSPHRASE": passphrase}
    if region := ctx.aws.region:
        env_vars["AWS_REGION"] = region
    if profile := ctx.aws.profile:
        env_vars["AWS_PROFILE"] = profile
    opts = LocalWorkspaceOptions(
        pulumi_command=PulumiCommand(str(get_stelvio_config_dir()), VersionInfo(3, 170, 0)),
        env_vars=env_vars,
        project_settings=project_settings,
        # pulumi_home if set is where pulumi installs plugins; otherwise it goes to ~/.pulumi
        pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
    )
    logger.debug("Creating stack")
    stack = create_or_select_stack(
        stack_name=stack_name,
        project_name=ctx.name,
        program=StelvioApp.get_instance()._get_pulumi_program_func(),  # noqa: SLF001
        opts=opts,
    )
    logger.debug("Successfully initialized stack")
    return stack


class CommandRun:
    def __init__(
        self,
        env: str,
        lock_as: str | None = None,
        *,
        state_only: bool = False,
        dev_mode: bool = False,
    ) -> None:
        self.env = env
        self.dev_mode = dev_mode
        self._lock_as = lock_as
        self._state_only = state_only
        self._locked = False
        self._home: Home | None = None
        self._app_name: str | None = None
        self._workdir: Path | None = None
        self._update_id: str | None = None
        self._stack: Stack | None = None
        self._push_thread: threading.Thread | None = None
        self._push_stop: threading.Event | None = None
        self._push_trigger: threading.Event | None = None
        self._last_pushed_hash: str | None = None
        self._had_state: bool = False

    def __enter__(self) -> Self:
        # 1. Load app, 2. Create home, 3. Init storage
        self._home, ctx = _setup_app_home_storage(self.env, self.dev_mode)
        self._app_name = ctx.name

        # 4. Get or create passphrase
        passphrase = _get_or_create_passphrase(self._home, self._app_name, self.env)
        # 5. Generate update ID and create workdir
        self._update_id = _generate_update_id()
        self._workdir = get_dot_stelvio_dir() / self._update_id
        self._workdir.mkdir(parents=True, exist_ok=True)
        # If anything fails after workdir creation, we need to clean up manually
        # because __exit__ only runs if __enter__ completes successfully
        try:
            # 6. Lock if needed
            if self._lock_as:
                self._lock()
                self._locked = True
            # 7. Pull state (save whether state existed on S3)
            self._had_state = self._pull()
            # 8. Create Pulumi stack (skip for state_only mode)
            if not self._state_only:
                self._stack = _create_stack(ctx, passphrase, self._workdir)
        except Exception:
            if self._locked:
                self._unlock()
            if not os.environ.get("STLV_NO_CLEANUP"):
                shutil.rmtree(self._workdir)
            raise
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        try:
            self._unlock()
        finally:
            if not os.environ.get("STLV_NO_CLEANUP"):
                shutil.rmtree(self._workdir)

        return False

    @property
    def stack(self) -> Stack:
        return self._stack

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def has_deployed(self) -> bool:
        """True if state existed on S3 when pulled."""
        return self._had_state

    def load_state(self) -> dict | None:
        """Load and return state data. Returns None if no state file."""
        if self._state_path.exists():
            return json.loads(self._state_path.read_text())
        return None

    def push_state(self, state: dict | None = None) -> None:
        """Push state to S3.

        Args:
            state: If provided, write this dict first. Otherwise push existing Pulumi state file.
        """
        key = STATE_KEY.format(app=self._app_name, env=self.env)
        if state is not None:
            state_file = self._workdir / "state.json"
            state_file.write_text(json.dumps(state, indent=2))
            self._home.write_file(key, state_file)
        else:
            self._home.write_file(key, self._state_path)

    def create_state_snapshot(self) -> None:
        """Create snapshot of current state."""
        key = SNAPSHOT_KEY.format(app=self._app_name, env=self.env, update_id=self._update_id)
        self._home.write_file(key, self._state_path)

    def cleanup_state(self) -> None:
        """Delete state file (after destroy when stack is empty)."""
        key = STATE_KEY.format(app=self._app_name, env=self.env)
        self._home.delete_file(key)

    def delete_snapshots(self) -> None:
        """Delete all snapshots for this app/env."""
        prefix = f"snapshot/{self._app_name}/{self.env}/"
        self._home.delete_prefix(prefix)

    def start_partial_push(self, interval: float = 5.0) -> None:
        """Start background thread that pushes state periodically.

        Pushes immediately when trigger_push() is called (event-driven), or after
        `interval` seconds if no events. Uses hash-based change detection to skip
        redundant pushes. This prevents data loss during long deploys.
        """
        if self._push_thread is not None:
            return  # Already running

        self._push_stop = threading.Event()
        self._push_trigger = threading.Event()
        self._push_thread = threading.Thread(
            target=self._partial_push_loop,
            args=(interval,),
            daemon=True,
        )
        self._push_thread.start()

    def trigger_push(self) -> None:
        """Signal background thread to push soon. Called when a resource completes."""
        if self._push_trigger:
            self._push_trigger.set()

    def stop_partial_push(self) -> None:
        """Stop the partial push thread."""
        if self._push_stop:
            self._push_stop.set()
        if self._push_trigger:
            self._push_trigger.set()  # Wake thread so it can see stop signal
        if self._push_thread:
            self._push_thread.join(timeout=2.0)
            self._push_thread = None
            self._push_stop = None
            self._push_trigger = None

    def _partial_push_loop(self, interval: float) -> None:
        """Push state when triggered or after interval timeout."""
        while not self._push_stop.is_set():
            # Wait for trigger (event-driven) or timeout (timer fallback)
            triggered = self._push_trigger.wait(timeout=interval)

            if self._push_stop.is_set():
                break

            if triggered:
                self._push_trigger.clear()
                logger.debug("Partial push: triggered by event")
            else:
                logger.debug("Partial push: timer fired (no events for %.1fs)", interval)

            self._push_if_changed()

    def _push_if_changed(self) -> None:
        """Push state only if hash differs from last push.

        Safety measures:
        - Validates JSON before pushing (catches partial/corrupted reads)
        - Copies to temp file before upload (avoids race with Pulumi writes)
        - Catches all exceptions (partial push failure shouldn't crash deploy)
        - Uses hash to avoid redundant pushes
        """
        try:
            if not self._state_path.exists():
                return

            data = self._state_path.read_bytes()

            # Validate JSON before pushing - catches partial writes from Pulumi
            try:
                json.loads(data)
            except json.JSONDecodeError:
                logger.debug("State file not valid JSON yet - skipping partial push")
                return

            current_hash = hashlib.sha256(data).hexdigest()
            if current_hash != self._last_pushed_hash:
                logger.debug("Partial push: state changed, uploading")
                # Copy to temp file to avoid race with Pulumi modifying state mid-upload
                temp_path = self._workdir / "state_push_temp.json"
                temp_path.write_bytes(data)
                key = STATE_KEY.format(app=self._app_name, env=self.env)
                self._home.write_file(key, temp_path)
                self._last_pushed_hash = current_hash
        except Exception:
            # Partial push failure is non-fatal - log and continue
            logger.warning("Partial push failed", exc_info=True)

    def event_handler(
        self, *, display: "RichDeploymentHandler | None" = None
    ) -> "Callable[[EngineEvent], None]":
        """Create event handler for Pulumi operations.

        Forwards events to the display handler for UI updates. If partial push
        is active, also triggers push when resources complete.
        """
        from pulumi.automation import EngineEvent

        def handler(event: EngineEvent) -> None:
            if display:
                display.handle_event(event)
            if event.res_outputs_event or event.res_op_failed_event:
                self.trigger_push()

        return handler

    def _pull(self) -> bool:
        """Pull state from Home to workdir. Returns True if state existed."""
        key = STATE_KEY.format(app=self._app_name, env=self.env)
        return self._home.read_file(key, self._state_path)

    def _lock(self) -> None:
        """Acquire lock. Raises StateLocked if already locked."""
        key = LOCK_KEY.format(app=self._app_name, env=self.env)

        if self._home.file_exists(key):
            lock_path = self._workdir / "existing_lock.json"
            self._home.read_file(key, lock_path)
            lock_info = json.loads(lock_path.read_text())
            raise StateLockedError(
                command=lock_info["command"],
                created=lock_info["created"],
                update_id=lock_info["update_id"],
                env=self.env,
            )

        # Create lock file
        lock_info = {
            "created": datetime.now(UTC).isoformat(),
            "update_id": self._update_id,
            "command": self._lock_as,
            "run_id": os.environ.get("STLV_RUN_ID"),  # CI integration
        }
        lock_path = self._workdir / "lock.json"
        lock_path.write_text(json.dumps(lock_info))
        self._home.write_file(key, lock_path)

        # Create update record (audit trail)
        self._create_update()

    def _create_update(self) -> None:
        """Create update record when operation starts."""
        update_info = {
            "id": self._update_id,
            "command": self._lock_as,
            "run_id": os.environ.get("STLV_RUN_ID"),  # CI integration
            "time_started": datetime.now(UTC).isoformat(),
            "time_completed": None,
            "errors": None,
        }
        update_path = self._workdir / "update.json"
        update_path.write_text(json.dumps(update_info))
        key = UPDATE_KEY.format(app=self._app_name, env=self.env, update_id=self._update_id)
        self._home.write_file(key, update_path)

    def complete_update(self, *, errors: list[str] | None = None) -> None:
        """Mark update as completed (call at end of successful operation)."""
        if not self._locked:
            return

        # Read current update record
        key = UPDATE_KEY.format(app=self._app_name, env=self.env, update_id=self._update_id)
        update_path = self._workdir / "update.json"
        self._home.read_file(key, update_path)
        update_info = json.loads(update_path.read_text())

        # Update with completion info
        update_info["time_completed"] = datetime.now(UTC).isoformat()
        update_info["errors"] = errors

        update_path.write_text(json.dumps(update_info))
        self._home.write_file(key, update_path)

    def _unlock(self) -> None:
        """Release lock."""
        if self._locked:
            key = LOCK_KEY.format(app=self._app_name, env=self.env)
            self._home.delete_file(key)
            self._locked = False

    @property
    def _state_path(self) -> Path:
        """Path to state file in workdir."""
        return self._workdir / ".pulumi" / "stacks" / self._app_name / f"{self.env}.json"
