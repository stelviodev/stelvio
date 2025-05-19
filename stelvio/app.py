import logging
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import TypeVar, final

from pulumi import Resource as PulumiResource
from pulumi import ResourceOptions, dynamic
from pulumi.dynamic import CreateResult

# Import cleanup functions for both functions and layers
from stelvio.aws.function.dependencies import (
    clean_function_active_dependencies_caches_file,
    clean_function_stale_dependency_caches,
)
from stelvio.aws.layer import (
    clean_layer_active_dependencies_caches_file,
    clean_layer_stale_dependency_caches,
)
from stelvio.component import Component, ComponentRegistry
from stelvio.link import LinkConfig

from .aws.function import Function
from .project import get_project_root

T = TypeVar("T", bound=PulumiResource)

logger = logging.getLogger(__name__)


class PostDeploymentProvider(dynamic.ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        return dynamic.CreateResult(id_="stlv-post-deployment", outs=props)


class PostDeploymentResource(dynamic.Resource):
    def __init__(self, name: str, props: dict, opts: ResourceOptions):
        logger.debug("Cleaning up stale dependency caches post-deployment")
        clean_function_stale_dependency_caches()
        clean_layer_stale_dependency_caches()
        provider = PostDeploymentProvider()
        super().__init__(provider, name, props or {}, opts)


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
        # Clean active cache tracking files at the start of the run
        clean_function_active_dependencies_caches_file()
        clean_layer_active_dependencies_caches_file()

        self._load_modules(self._modules, get_project_root())
        # Brm brm, vroooom through those infrastructure deployments
        # like an Alfa Romeo through those Stelvio hairpins
        for i in ComponentRegistry.all_instances():
            _ = i.resources

        # This is temporary until we move to automation api
        all_functions_components = list(ComponentRegistry.instances_of(Function))
        all_pulumi_functions = [f.resources.function for f in all_functions_components]
        PostDeploymentResource(
            "stlv-post-deployment", props={}, opts=ResourceOptions(depends_on=all_pulumi_functions)
        )

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
