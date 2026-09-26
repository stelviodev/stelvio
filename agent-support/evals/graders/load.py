"""Load a Stelvio project directory and run its @app.run body without deploying."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from stelvio.app import StelvioApp
from stelvio.component import ComponentRegistry
from stelvio.project import get_project_root


def reset_stelvio_app() -> None:
    StelvioApp._StelvioApp__instance = None


def _unload_project_modules(project_dir: Path) -> None:
    root = str(project_dir.resolve())
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None) or next(
            iter(getattr(module, "__path__", None) or []),
            None,
        )
        if name == "stlv_resources" or (path and str(path).startswith(root)):
            del sys.modules[name]


def load_and_run_app(project_dir: Path) -> ModuleType:
    """Import ``stlv_app.py`` from ``project_dir`` and execute ``@app.run``.

    Requires AppContext / ProviderStore to already be set (test fixtures).
    Does not call ``drive()`` / ``.resources`` — component graphs are inspected
    after construction only.
    """
    project_dir = project_dir.resolve()
    app_path = project_dir / "stlv_app.py"
    if not app_path.is_file():
        raise FileNotFoundError(f"No stlv_app.py in {project_dir}")

    reset_stelvio_app()
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
    get_project_root.cache_clear()
    _unload_project_modules(project_dir)

    inserted = False
    project_str = str(project_dir)
    if project_str not in sys.path:
        sys.path.insert(0, project_str)
        inserted = True

    module_name = f"_stelvio_eval_app_{project_dir.name}_{id(project_dir)}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, app_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {app_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        app = StelvioApp.get_instance()
        if app._run_func is None:
            raise RuntimeError(f"{app_path} has no @app.run function")
        app._run_func()
        return module
    finally:
        if inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(project_str)
        get_project_root.cache_clear()
