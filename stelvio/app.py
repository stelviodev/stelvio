from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import TypeVar, final

from link import LinkConfig
from pulumi import Resource as PulumiResource

from stelvio.aws.function import get_project_root
from stelvio.component import Component, ComponentRegistry

T = TypeVar("T", bound=PulumiResource)


@final
class StelvioApp:
    def __init__(
        self,
        name: str,
        modules: list[str],
        link_configs: dict[type[Component[T]], Callable[[T], LinkConfig]] | None = None,
    ):
        self.name = name
        self._modules = modules
        if link_configs:
            for component_type, fn in link_configs.items():
                self.set_user_link_for(component_type, fn)

    @staticmethod
    def set_user_link_for(
        component_type: type[Component[T]], func: Callable[[T], LinkConfig]
    ) -> None:
        """Register a user-defined link creator that overrides defaults"""
        ComponentRegistry.register_user_link_creator(component_type, func)

    def run(self) -> None:
        self.drive()

    def drive(self) -> None:
        self._load_modules(self._modules, get_project_root())
        # Vroooom through those infrastructure deployments
        # like an Alfa through those hairpins
        for i in ComponentRegistry.all_instances():
            i._ensure_resource()  # noqa: SLF001

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
