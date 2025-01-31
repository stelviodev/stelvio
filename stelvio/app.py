import glob
from importlib import import_module
from pathlib import Path
from typing import Callable, final

from stelvio.aws.function import get_project_root
from stelvio.component import ComponentRegistry


@final
class StelvioApp:
    def __init__(
        self,
        name: str,
        modules: list[str],
        link_configs: dict[type, Callable] | None = None,
    ):
        self.name = name
        self._modules = modules
        if link_configs:
            for component_type, fn in link_configs.items():
                self.set_default_link_for(component_type, fn)

    @staticmethod
    def set_default_link_for(component_type, func):
        ComponentRegistry.register_link_config_creator(component_type, func)

    def run(self):
        self.drive()

    def drive(self):
        self._load_modules(self._modules, get_project_root())
        # Vroooom through those infrastructure deployments
        # like an Alfa through those hairpins
        for i in ComponentRegistry.all_instances():
            print(i.name)
            i._ensure_resource()

    def _load_modules(self, modules: list[str], project_root: Path):
        exclude_dirs = {"__pycache__", "build", "dist", "node_modules", ".egg-info"}

        for pattern in modules:
            # Direct module import
            if "." in pattern and not any(c in pattern for c in "/*?[]"):
                import_module(pattern)
                continue

            # Glob pattern
            files = glob.glob(pattern, root_dir=project_root, recursive=True)

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
