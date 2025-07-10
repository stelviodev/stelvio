import logging
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import ClassVar, TypeVar, final

from pulumi import Resource as PulumiResource

# Import cleanup functions for both functions and layers
from stelvio.component import Component, ComponentRegistry
from stelvio.config import StelvioAppConfig
from stelvio.link import LinkConfig

from .project import get_project_root

T = TypeVar("T", bound=PulumiResource)

logger = logging.getLogger(__name__)


type StelvioConfigFn = Callable[[str], StelvioAppConfig]


@final
class StelvioApp:
    __instance: ClassVar["StelvioApp | None"] = None

    def __init__(
        self,
        name: str,
        modules: list[str] | None = None,
        link_configs: dict[type[Component[T]], Callable[[T], LinkConfig]] | None = None,
    ):
        if StelvioApp.__instance is not None:
            raise RuntimeError("StelvioApp has already been instantiated.")

        self._name = name
        self._modules = modules or []
        self._config_func = None
        self._run_func = None
        if link_configs:
            for component_type, fn in link_configs.items():
                self.set_user_link_for(component_type, fn)
        if StelvioApp.__instance:
            raise RuntimeError("StelvioApp instance already exists. Only one is allowed.")
        StelvioApp.__instance = self

    @classmethod
    def get_instance(cls) -> "StelvioApp":
        if cls.__instance is None:
            raise RuntimeError(
                "StelvioApp has not been instantiated. Ensure 'app = StelvioApp(...)' is called "
                "in your stlv_app.py."
            )
        return cls.__instance

    def config(self, func: StelvioConfigFn) -> StelvioConfigFn:
        if self._config_func:
            raise RuntimeError("Config function already registered.")
        self._config_func = func
        logger.debug("Config function '%s' registered for app '%s'.", func.__name__, self._name)
        return func

    def run(self, func: Callable[[], None]) -> Callable[[], None]:
        if self._run_func:
            raise RuntimeError("Run function already registered.")
        self._run_func = func
        logger.debug("Run function '%s' registered for app '%s'.", func.__name__, self._name)
        return func

    def _execute_user_config_func(self, env: str) -> StelvioAppConfig:
        if not self._config_func:
            raise RuntimeError("No @StelvioApp.config function defined.")
        self._app_config: StelvioAppConfig = self._config_func(env)
        if self._app_config is None or not isinstance(self._app_config, StelvioAppConfig):
            raise ValueError("@app.config function must return an instance of StelvioAppConfig.")
        return self._app_config

    def _get_pulumi_program_func(self) -> Callable[[], None]:
        if not self._run_func:
            raise RuntimeError("No @StelvioApp.run function defined.")

        def run() -> None:
            self._run_func()
            self.drive()

        return run

    @staticmethod
    def set_user_link_for(
        component_type: type[Component[T]], func: Callable[[T], LinkConfig]
    ) -> None:
        """Register a user-defined link creator that overrides defaults"""
        ComponentRegistry.register_user_link_creator(component_type, func)

    def drive(self) -> None:
        self._load_modules(self._modules, get_project_root())
        # Brm brm, vroooom through those infrastructure deployments
        # like an Alfa Romeo through those Stelvio hairpins
        for i in ComponentRegistry.all_instances():
            _ = i.resources

    def _load_modules(self, modules: list[str], project_root: Path) -> None:
        exclude_dirs = {"__pycache__", "build", "dist", "node_modules", ".egg-info"}
        for pattern in modules:
            # Direct module import
            if "." in pattern and not any(c in pattern for c in "/*?[]"):
                import_module(pattern)
                continue

            # Glob pattern
            files = project_root.rglob(pattern)

            for file in files:
                path = Path(file)

                # Skip hidden folders (any part starts with .)
                if any(part.startswith(".") for part in path.parts):
                    continue

                if path.suffix == ".py" and not any(
                    excluded in path.parts for excluded in exclude_dirs
                ):
                    parts = list(path.with_suffix("").parts)
                    if all(part.isidentifier() for part in parts):
                        module_path = ".".join(parts)
                        import_module(module_path)
