"""App-owned dispatch and command lifecycle coordination."""

from __future__ import annotations

import asyncio
import inspect
import signal
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any

from click import Abort

from stelvio import signals
from stelvio.component import ComponentRegistry
from stelvio.signals import (
    CancelEvent,
    DevBridgeEvent,
    ErrorEvent,
    ErrorPolicy,
    OperationEvent,
    OperationName,
    OperationOptions,
    OperationResult,
    PhaseEvent,
    PhaseName,
    Signal,
    _copy_config,
    _Inputs,
)

if TYPE_CHECKING:
    from types import FrameType

    from stelvio.app import StelvioApp
    from stelvio.config import StelvioAppConfig

type Handler[EventT] = Callable[[EventT], object | Awaitable[object]]


@dataclass(frozen=True)
class _Receiver:
    handler: Handler[Any]
    errors: ErrorPolicy


def _register[EventT](
    app: StelvioApp, signal: Signal[EventT], handler: Handler[EventT], errors: ErrorPolicy
) -> None:
    if not any(signal is builtin for builtin in signals._BUILTINS):  # noqa: SLF001
        raise ValueError("Only built-in Stelvio lifecycle signals are supported.")
    if errors not in ("fail", "continue"):
        raise ValueError("errors must be 'fail' or 'continue'")
    if not callable(handler):
        raise TypeError("signal handler must be callable")
    receivers = app._signal_handlers.setdefault(signal, [])  # noqa: SLF001
    if not any(receiver.handler == handler for receiver in receivers):
        receivers.append(_Receiver(handler, errors))


def _diagnostic(error: BaseException, *, handler: str | None = None) -> None:
    prefix = f"Signal handler {handler}" if handler else "Lifecycle cleanup"
    sys.stderr.write(f"{prefix} failed: {type(error).__name__}: {error}\n")


async def _dispatch(
    app: StelvioApp,
    event: OperationEvent,
    *,
    robust: bool = False,
    receivers: tuple[_Receiver, ...] | None = None,
    phase: str | None = None,
) -> None:
    if receivers is None:
        receivers = tuple(app._signal_handlers.get(event.signal, ()))  # noqa: SLF001
    for receiver in receivers:
        name = getattr(receiver.handler, "__qualname__", type(receiver.handler).__name__)
        try:
            response = receiver.handler(event)
            if inspect.isawaitable(response):
                await response
        except BaseException as error:
            if robust or (
                isinstance(error, Exception)
                and not isinstance(error, Abort)
                and receiver.errors == "continue"
            ):
                _diagnostic(error, handler=name)
                continue
            session = _current.get()
            if session is not None:
                session.handler = name
                session.phase = phase or event.signal.name
            raise


def _dispatch_sync(
    app: StelvioApp,
    event: OperationEvent,
    *,
    robust: bool = False,
    receivers: tuple[_Receiver, ...] | None = None,
    phase: str | None = None,
) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Use async lifecycle dispatch inside an active event loop.")
    try:
        previous_loop = asyncio.get_event_loop()
    except RuntimeError:
        previous_loop = None
    try:
        with asyncio.Runner() as runner:
            runner.run(_dispatch(app, event, robust=robust, receivers=receivers, phase=phase))
    finally:
        asyncio.set_event_loop(previous_loop)


_EVENT_TYPES = {
    "deploy": signals.DeployEvent,
    "diff": signals.DiffEvent,
    "destroy": signals.DestroyEvent,
    "refresh": signals.RefreshEvent,
    "dev": signals.DevEvent,
}
_current: ContextVar[_Session | None] = ContextVar("stelvio_signal_session", default=None)


class _Session:
    def __init__(self, command: OperationName, env: str, options: OperationOptions):
        self.command = command
        self.operation = command
        self.env = env
        self.inputs = _Inputs(options=options)
        self.app: StelvioApp | None = None
        self.prepared = False
        self.result: OperationResult | None = None
        self.update_id: str | None = None
        self.phase = "config"
        self.handler: str | None = None
        self.failure: BaseException | None = None
        self.failure_phase: str | None = None
        self.failure_handler: str | None = None
        self.interruption_phase: str | None = None
        self.cancelled = False
        self.bridge_ready = False
        self.completion: Callable[[], object] | None = None
        self.completion_emitted = False
        self.failed_completion: Callable[[BaseException], object] | None = None
        self.state_exists: bool | None = None
        self.stack_name: str | None = None
        self.recorded_errors: tuple[str, ...] = ()
        self.secondary_failures: list[BaseException] = []
        self.local_components: object | None = None
        self.local_callbacks: list[_Receiver] = []

    def event(self, signal: Signal[Any], **extra: object) -> OperationEvent:
        event_type = _EVENT_TYPES[self.operation]
        inputs = self.inputs
        if signal is signals.on_error:
            event_type = ErrorEvent
        elif signal is signals.on_cancel:
            event_type = CancelEvent
        elif "bridge" in signal.name:
            event_type = DevBridgeEvent
            extra["ready"] = self.bridge_ready
        elif signal in signals._PHASE_SIGNALS:  # noqa: SLF001
            event_type = PhaseEvent
            inputs = _Inputs(
                config=self.inputs.config,
                options=self.inputs.options,
                config_editable=self.inputs.config_editable,
            )
            phase = signals._PHASE_SIGNALS[signal]  # noqa: SLF001
            extra.update(
                phase=phase,
                backend=self.inputs.config.home if self.inputs.config else None,
                state_exists=self.state_exists,
                stack_name=self.stack_name,
                snapshot_id=self.update_id if phase == "snapshot_create" else None,
                errors=self.recorded_errors if phase == "update_complete" else (),
            )
            if phase in ("app_run", "resource_creation"):
                extra["components"] = tuple(ComponentRegistry.all_instances())
        return event_type(
            signal=signal,
            app_name=self.app._name,  # noqa: SLF001
            env=self.env,
            command=self.command,
            operation=self.operation,
            dev_mode=self.command == "dev",
            update_id=self.update_id,
            result=deepcopy(self.result),
            _inputs=inputs,
            **extra,
        )

    def prepared_config(self, app: StelvioApp, config: StelvioAppConfig) -> StelvioAppConfig:
        if self.prepared:
            return self.inputs.config
        self.app = app
        self.inputs.config = _copy_config(config)
        self.inputs.editable = True
        try:
            self.emit(getattr(signals, f"before_{self.command}"))
            if self.command == "dev":
                self.operation = "deploy"
                self.emit(signals.before_deploy)
        finally:
            self.inputs.editable = False
        self.prepared = True
        self.phase = "setup"
        return self.inputs.config

    def prepare(self) -> None:
        if self.prepared:
            return
        from stelvio.command_run import (  # noqa: PLC0415  # command_run imports this module
            _PRELOADED_APP_CONFIGS,
            _load_app_config,
            _validate_environment,
        )

        app, config = _load_app_config(self.env)
        _validate_environment(config, self.env)
        _PRELOADED_APP_CONFIGS[self.env] = (app, config)

    def emit(self, signal: Signal[Any], *, robust: bool = False, **extra: object) -> None:
        if self.app is not None:
            self.phase = signal.name
            self.handler = None
            event = self.event(signal, **extra)
            try:
                _dispatch_sync(self.app, event, robust=robust)
            finally:
                if event._inputs.config_editable:  # noqa: SLF001
                    self.inputs.config = event._inputs.config  # noqa: SLF001
                    event._inputs.config_editable = False  # noqa: SLF001

    async def emit_async(self, signal: Signal[Any]) -> None:
        if self.app is not None:
            self.phase = signal.name
            self.handler = None
            await _dispatch(self.app, self.event(signal))

    def cancel(self, reason: str) -> None:
        if not self.cancelled:
            self.cancelled = True
            phase = self.interruption_phase if reason == "interrupted" else None
            self.emit(signals.on_cancel, robust=True, reason=reason, phase=phase or self.phase)

    def fail(self, error: BaseException) -> None:
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError, Abort)):
            if self.interruption_phase is None:
                self.interruption_phase = self.phase
            return
        if isinstance(error, SystemExit) and self.failure is not None:
            return
        if self.interruption_phase is not None and self.failure is None:
            if not any(error is item for item in self.secondary_failures):
                self.secondary_failures.append(error)
                _diagnostic(error)
            return
        if self.failure is None:
            self.failure = error
            self.failure_phase = self.phase
            self.failure_handler = self.handler
        elif self.failure is not error and not any(
            error is item for item in self.secondary_failures
        ):
            self.secondary_failures.append(error)
            _diagnostic(error)

    def notify_failure(self, error: BaseException) -> None:
        if self.app is None:
            from stelvio.app import StelvioApp  # noqa: PLC0415  # app imports this module

            try:
                self.app = StelvioApp.get_instance()
            except RuntimeError:
                return
        self.emit(
            signals.on_error,
            robust=True,
            error=self.failure or error,
            phase=self.failure_phase or self.phase,
            handler=self.failure_handler or self.handler,
        )


def _interrupt_local_execution(signum: int, frame: FrameType | None) -> None:
    signal.default_int_handler(signum, frame)


@contextmanager
def _local_execution_context() -> Iterator[None]:
    restore_signal = (
        threading.current_thread() is threading.main_thread()
        and signal.getsignal(signal.SIGINT) is signal.default_int_handler
    )
    if restore_signal:
        # Runner's task cancellation cannot interrupt synchronous input() on its first SIGINT.
        signal.signal(signal.SIGINT, _interrupt_local_execution)
    suppression = sys.modules.get("stelvio._suppress_grpc")
    try:
        # Importing the app without the CLI must not enable fd-level gRPC suppression.
        with suppression._native_stderr() if suppression is not None else nullcontext():  # noqa: SLF001
            yield
    finally:
        if restore_signal:
            signal.signal(signal.SIGINT, signal.default_int_handler)


def _complete_operation(session: _Session) -> None:
    session.emit(getattr(signals, f"after_{session.command}"))
    if session.completion is not None:
        session.phase = "completion"
        session.handler = None
        session.completion()
        session.completion_emitted = True
    if session.local_callbacks:
        session.phase = "local_execute"
        session.handler = None
        callbacks = tuple(session.local_callbacks)
        session.local_callbacks.clear()
        with _local_execution_context():
            _dispatch_sync(
                session.app,
                session.event(signals.after_deploy),
                receivers=callbacks,
                phase="local_execute",
            )


@contextmanager
def _command_scope(
    command: OperationName, env: str, *, show_unchanged: bool = False, compact: bool = False
) -> Iterator[_Session]:
    active = _current.get()
    if active is not None:
        yield active
        return
    session = _Session(
        command, env, OperationOptions(show_unchanged=show_unchanged, compact=compact)
    )
    token = _current.set(session)
    try:
        yield session
        if session.prepared and not session.cancelled:
            _complete_operation(session)
    except (KeyboardInterrupt, asyncio.CancelledError, Abort):
        session.cancel("interrupted")
        raise
    except BaseException as error:
        session.fail(error)
        session.notify_failure(error)
        if session.completion_emitted:
            _diagnostic(error, handler=session.failure_handler)
            error._stelvio_lifecycle_reported = True  # noqa: SLF001
        elif session.failed_completion is not None:
            try:
                session.failed_completion(session.failure or error)
                error._stelvio_lifecycle_reported = True  # noqa: SLF001
            except BaseException as secondary:
                _diagnostic(secondary)
        if isinstance(error, SystemExit) and not error.code:
            raise SystemExit(1) from error
        raise
    finally:
        from stelvio.command_run import _PRELOADED_APP_CONFIGS  # noqa: PLC0415

        _PRELOADED_APP_CONFIGS.pop(env, None)
        _current.reset(token)


def _operation[**P, R](command: OperationName) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(func)

        @wraps(func)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            with _command_scope(
                command,
                bound.arguments["env"],
                show_unchanged=bound.arguments.get("show_unchanged", False),
                compact=bound.arguments.get("compact", False),
            ) as session:
                session.prepare()
                for name in ("show_unchanged", "compact"):
                    if name in bound.arguments:
                        bound.arguments[name] = getattr(session.inputs.options, name)
                session.phase = "setup"
                return func(*bound.args, **bound.kwargs)

        return wrapped

    return decorate


def _configured(app: StelvioApp, config: StelvioAppConfig) -> StelvioAppConfig:
    session = _current.get()
    return session.prepared_config(app, config) if session is not None else config


def _record_result(*, outputs: dict[str, Any] | None = None, no_op: bool = False) -> None:
    session = _current.get()
    if session is not None:
        session.result = OperationResult(no_op=no_op, outputs=outputs or {})


def _record_failure(error: BaseException) -> None:
    session = _current.get()
    if session is not None:
        session.fail(error)


def _cancel_confirmation() -> None:
    session = _current.get()
    if session is not None:
        session.cancel("confirmation_declined")


def _recover(
    steps: list[tuple[str, Callable[[], object]]], primary: BaseException | None
) -> BaseException | None:
    if primary is not None:
        _record_failure(primary)
    for phase, action in steps:
        if session := _current.get():
            session.phase = phase
        try:
            action()
        except BaseException as error:
            if primary is None:
                primary = error
                _record_failure(error)
            else:
                _record_failure(error) if _current.get() is not None else _diagnostic(error)
    return primary


def _run_phase[R](phase: PhaseName, action: Callable[[], R], *, required: bool = False) -> R:
    session = _current.get()
    if session is None or session.app is None:
        return action()
    before_error: BaseException | None = None
    try:
        session.emit(getattr(signals, f"before_{phase}"))
    except BaseException as error:
        _record_failure(error)
        if not required:
            raise
        before_error = error
    session.phase = phase
    session.handler = None
    try:
        result = action()
        if phase == "config":
            session.inputs.config = _copy_config(result)
        elif phase == "state_load":
            session.state_exists = result
        session.inputs.config_editable = phase == "config"
        try:
            session.emit(getattr(signals, f"after_{phase}"))
        finally:
            session.inputs.config_editable = False
    except BaseException as error:
        _record_failure(error)
        if before_error is not None:
            raise before_error from error
        raise
    if before_error is not None:
        raise before_error
    return session.inputs.config if phase == "config" else result


def _phased[**P, R](
    phase: PhaseName, *, required: bool = False
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            if phase == "update_complete" and (session := _current.get()) is not None:
                errors = kwargs.get("errors")
                session.recorded_errors = tuple(
                    errors or ([str(session.failure)] if session.failure is not None else [])
                )
            return _run_phase(phase, lambda: func(*args, **kwargs), required=required)

        return wrapped

    return decorate


@asynccontextmanager
async def _program_phase(phase: PhaseName) -> AsyncIterator[None]:
    session = _current.get()
    parent = (session.phase, session.handler) if session is not None else None
    try:
        if session is not None:
            await session.emit_async(getattr(signals, f"before_{phase}"))
            session.phase = phase
            session.handler = None
        yield
        if session is not None:
            await session.emit_async(getattr(signals, f"after_{phase}"))
    except BaseException as error:
        _record_failure(error)
        raise
    else:
        if session is not None and parent is not None:
            session.phase, session.handler = parent
