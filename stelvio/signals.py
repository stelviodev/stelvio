"""Typed lifecycle events and built-in signals for ``StelvioApp.on``."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast, final

from stelvio.config import StelvioAppConfig

type ErrorPolicy = Literal["fail", "continue"]
type OperationName = Literal["deploy", "diff", "destroy", "refresh", "dev"]


@final
@dataclass(frozen=True)
class Signal[EventT]:
    name: str


@final
@dataclass(frozen=True, kw_only=True)
class OperationOptions:
    show_unchanged: bool = False
    compact: bool = False


@final
@dataclass(frozen=True, kw_only=True)
class OperationResult:
    no_op: bool = False
    outputs: dict[str, Any] = field(default_factory=dict)


def _copy_config(config: StelvioAppConfig) -> StelvioAppConfig:
    # DNS providers and customization callables keep their identity; containers do not.
    return replace(
        config,
        tags=dict(config.tags),
        environments=list(config.environments),
        customize=_copy_containers(config.customize),
    )


def _copy_containers[T](value: T) -> T:
    # Pulumi Outputs and resources cannot be deep-copied outside serialization.
    if isinstance(value, dict):
        return cast("T", {key: _copy_containers(item) for key, item in value.items()})
    if isinstance(value, list):
        return cast("T", [_copy_containers(item) for item in value])
    if isinstance(value, tuple):
        return cast("T", tuple(_copy_containers(item) for item in value))
    return value


@dataclass(kw_only=True)
class _Inputs:
    config: StelvioAppConfig | None = None
    options: OperationOptions = field(default_factory=OperationOptions)
    editable: bool = False


@dataclass(frozen=True, kw_only=True)
class OperationEvent:
    signal: Signal[Any]
    app_name: str
    env: str
    command: OperationName
    operation: OperationName
    dev_mode: bool
    update_id: str | None = None
    result: OperationResult | None = None
    _inputs: _Inputs = field(default_factory=_Inputs, repr=False, compare=False)

    @property
    def config(self) -> StelvioAppConfig | None:
        config = self._inputs.config
        return _copy_config(config) if config is not None else None

    @property
    def options(self) -> OperationOptions:
        return self._inputs.options

    @property
    def outputs(self) -> dict[str, Any]:
        return deepcopy(self.result.outputs) if self.result else {}

    def _check_editable(self) -> None:
        if not self.signal.name.startswith("before_") or not self._inputs.editable:
            raise RuntimeError("Inputs can only change during before-operation handlers.")

    def replace_config(self, config: StelvioAppConfig) -> None:
        self._check_editable()
        if not isinstance(config, StelvioAppConfig):
            raise TypeError("config must be a StelvioAppConfig")
        self._inputs.config = _copy_config(config)

    def update_options(
        self, *, show_unchanged: bool | None = None, compact: bool | None = None
    ) -> None:
        self._check_editable()
        changes = {"show_unchanged": show_unchanged, "compact": compact}
        for name, value in changes.items():
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
        if compact is not None and self.operation != "diff":
            raise ValueError("compact is only supported for diff")
        if show_unchanged is not None and self.operation not in ("deploy", "diff", "dev"):
            raise ValueError("show_unchanged is only supported for deploy, diff, and dev")
        self._inputs.options = replace(
            self.options, **{name: value for name, value in changes.items() if value is not None}
        )


@final
@dataclass(frozen=True, kw_only=True)
class DeployEvent(OperationEvent):
    pass


@final
@dataclass(frozen=True, kw_only=True)
class DiffEvent(OperationEvent):
    pass


@final
@dataclass(frozen=True, kw_only=True)
class DestroyEvent(OperationEvent):
    pass


@final
@dataclass(frozen=True, kw_only=True)
class RefreshEvent(OperationEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class DevEvent(OperationEvent):
    pass


@final
@dataclass(frozen=True, kw_only=True)
class DevBridgeEvent(DevEvent):
    ready: bool = False


@final
@dataclass(frozen=True, kw_only=True)
class ErrorEvent(OperationEvent):
    error: BaseException
    phase: str
    handler: str | None = None


@final
@dataclass(frozen=True, kw_only=True)
class CancelEvent(OperationEvent):
    reason: Literal["interrupted", "confirmation_declined"]
    phase: str


before_deploy = Signal[DeployEvent]("before_deploy")
after_deploy = Signal[DeployEvent]("after_deploy")
before_diff = Signal[DiffEvent]("before_diff")
after_diff = Signal[DiffEvent]("after_diff")
before_destroy = Signal[DestroyEvent]("before_destroy")
after_destroy = Signal[DestroyEvent]("after_destroy")
before_refresh = Signal[RefreshEvent]("before_refresh")
after_refresh = Signal[RefreshEvent]("after_refresh")
before_dev = Signal[DevEvent]("before_dev")
after_dev = Signal[DevEvent]("after_dev")
before_dev_bridge_start = Signal[DevBridgeEvent]("before_dev_bridge_start")
after_dev_bridge_start = Signal[DevBridgeEvent]("after_dev_bridge_start")
before_dev_bridge_stop = Signal[DevBridgeEvent]("before_dev_bridge_stop")
after_dev_bridge_stop = Signal[DevBridgeEvent]("after_dev_bridge_stop")
on_error = Signal[ErrorEvent]("on_error")
on_cancel = Signal[CancelEvent]("on_cancel")

_BUILTINS = (
    before_deploy,
    after_deploy,
    before_diff,
    after_diff,
    before_destroy,
    after_destroy,
    before_refresh,
    after_refresh,
    before_dev,
    after_dev,
    before_dev_bridge_start,
    after_dev_bridge_start,
    before_dev_bridge_stop,
    after_dev_bridge_stop,
    on_error,
    on_cancel,
)
