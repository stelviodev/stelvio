"""`stlv dev` runs every handler in one interpreter through `Function.handle_bridge_event`.
These tests drive that seam with handler files written into the project copy, the way the
dev server does after `stack.up`."""

import json
import os
import py_compile
import re
import sys
from pathlib import Path

import pulumi
from pytest import mark, param

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.api_gateway.rest_api.config import CorsConfig
from stelvio.aws.function import Function
from stelvio.link import Link

pytestmark = mark.usefixtures("project_cwd", "pulumi_mocks", "dev_mode_context")


HANDLER_OK = "def main(event, context):\n    return {}\n"
LAMBDA_CONTEXT = {
    "invoke_id": "invoke-1",
    "client_context": None,
    "cognito_identity": None,
    "epoch_deadline_time_in_ms": 0,
}


def write_files(project: Path, files: dict[str, str]) -> None:
    for relative_path, source in files.items():
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


async def invoke(function: Function, **message: object) -> object:
    event = {
        "endpointId": function._dev_endpoint_id,
        "event": {"path": "/", "httpMethod": "GET"},
        "context": LAMBDA_CONTEXT,
        **message,
    }
    return await function.handle_bridge_event({"type": "data", "event": json.dumps(event)})


async def invoke_ok(function: Function, **message: object) -> dict:
    result = await invoke(function, **message)
    assert result.error_result is None
    return result.success_result


def table_link(name: str) -> Link:
    return Link("table", properties={"table_name": name}, permissions=[])


@pulumi.runtime.test
def test_dev_mode_handler_receives_lambda_context(project_cwd):
    """Bridge event context is unpacked into LambdaContext before the handler runs."""
    write_files(
        project_cwd,
        {
            "functions/ctx.py": (
                "def main(event, context):\n"
                "    return {\n"
                "        'id': context.aws_request_id,\n"
                "        'remaining': context.get_remaining_time_in_millis(),\n"
                "        'identity': context.identity.cognito_identity_id,\n"
                "    }\n"
            )
        },
    )
    fn = Function("fn", handler="functions/ctx.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn) == {"id": "invoke-1", "remaining": 0, "identity": None}

    return check()


HANDLER_LAMBDA_VALUES = (
    "import os\n"
    "def main(event, context):\n"
    "    return {\n"
    "        'name': context.function_name,\n"
    "        'version': context.function_version,\n"
    "        'memory': context.memory_limit_in_mb,\n"
    "        'log_group': context.log_group_name,\n"
    "        'log_stream': context.log_stream_name,\n"
    "        'env_name': os.environ.get('AWS_LAMBDA_FUNCTION_NAME'),\n"
    "    }\n"
)


@pulumi.runtime.test
def test_dev_mode_handler_sees_the_deployed_functions_lambda_values(project_cwd):
    write_files(project_cwd, {"functions/values.py": HANDLER_LAMBDA_VALUES})
    fn = Function("fn", handler="functions/values.main", memory=512)
    _ = fn.resources
    lambda_env = {
        "AWS_LAMBDA_FUNCTION_NAME": "app-env-fn-1a2b",
        "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
        "AWS_LAMBDA_LOG_GROUP_NAME": "/aws/lambda/app-env-fn-1a2b",
        "AWS_LAMBDA_LOG_STREAM_NAME": "2026/10/27/[$LATEST]abc",
    }

    async def check():
        assert await invoke_ok(fn, lambdaEnv=lambda_env) == {
            "name": "app-env-fn-1a2b",
            "version": "$LATEST",
            "memory": "512",
            "log_group": "/aws/lambda/app-env-fn-1a2b",
            "log_stream": "2026/10/27/[$LATEST]abc",
            "env_name": "app-env-fn-1a2b",
        }

    return check()


@pulumi.runtime.test
def test_dev_mode_takes_only_the_stub_keys_from_a_bridge_message(project_cwd, monkeypatch):
    """Anyone with the AppSync key can publish a message; it must not point the handler,
    which runs with the developer's credentials, at another profile or endpoint."""
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    write_files(
        project_cwd,
        {
            "functions/env.py": (
                "import os\n"
                "def main(event, context):\n"
                "    return {k: os.environ.get(k) for k in ('AWS_LAMBDA_FUNCTION_NAME',"
                " 'AWS_LAMBDA_FUNCTION_VERSION', 'AWS_PROFILE', 'AWS_ENDPOINT_URL')}\n"
            )
        },
    )
    fn = Function("fn", handler="functions/env.main")
    _ = fn.resources
    lambda_env = {
        "AWS_LAMBDA_FUNCTION_NAME": "app-env-fn-1a2b",
        "AWS_LAMBDA_FUNCTION_VERSION": 7,
        "AWS_PROFILE": "attacker",
        "AWS_ENDPOINT_URL": "https://attacker.example",
    }

    async def check():
        assert await invoke_ok(fn, lambdaEnv=lambda_env) == {
            "AWS_LAMBDA_FUNCTION_NAME": "app-env-fn-1a2b",
            "AWS_LAMBDA_FUNCTION_VERSION": "7",
            "AWS_PROFILE": "default",
            "AWS_ENDPOINT_URL": None,
        }

    return check()


@pulumi.runtime.test
def test_dev_mode_message_from_an_older_stub_leaves_lambda_names_unset(project_cwd, monkeypatch):
    for key in (
        "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_LAMBDA_FUNCTION_VERSION",
        "AWS_LAMBDA_LOG_GROUP_NAME",
        "AWS_LAMBDA_LOG_STREAM_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    write_files(project_cwd, {"functions/values.py": HANDLER_LAMBDA_VALUES})
    fn = Function("fn", handler="functions/values.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn) == {
            "name": None,
            "version": None,
            "memory": "128",
            "log_group": None,
            "log_stream": None,
            "env_name": None,
        }

    return check()


@mark.parametrize("lambda_env", [7, "AWS_LAMBDA_FUNCTION_NAME", ["AWS_LAMBDA_FUNCTION_NAME"]])
@pulumi.runtime.test
def test_dev_mode_ignores_a_lambda_env_that_is_not_a_dict(project_cwd, lambda_env):
    """Only a message somebody else published has one; it must not stop the request."""
    write_files(project_cwd, {"functions/ok.py": HANDLER_OK})
    fn = Function("fn", handler="functions/ok.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn, lambdaEnv=lambda_env) == {}

    return check()


@pulumi.runtime.test
def test_dev_mode_each_call_sees_its_own_function(project_cwd):
    """Two folders with the same link name, CORS origin and a same-named `utils` module:
    the second function called must not get the first one's cached modules, and an edited
    helper must load on the next call."""
    handler = (
        "import utils\n"
        "from stlv_resources import Resources\n"
        "def main(event, context):\n"
        "    return {'statusCode': 200, 'body': {'table': Resources.table.table_name,"
        " 'origin': Resources.cors.allow_origin, 'utils': utils.WHO}}\n"
    )
    write_files(
        project_cwd,
        {
            "functions/a/handler.py": handler,
            "functions/a/utils.py": "WHO = 'a-v1'\n",
            "functions/b/handler.py": handler,
            "functions/b/utils.py": "WHO = 'b-v1'\n",
        },
    )
    # a pyc from before `stlv dev` started (pytest, a script): the first call runs it, and a
    # same-size same-mtime edit would still pass its header check
    py_compile.compile(
        str(project_cwd / "functions/b/utils.py"),
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    functions = {}
    for folder in ("a", "b"):
        fn = Function(
            f"fn-{folder}",
            handler=f"functions/{folder}::handler.main",
            links=[table_link(f"table-{folder}")],
        )
        RestApi(f"api-{folder}", cors=CorsConfig(allow_origins=f"https://{folder}.example")).route(
            "GET", "/", fn
        )
        _ = fn.resources
        functions[folder] = fn

    async def check():
        result_a = await invoke_ok(functions["a"])
        result_b = await invoke_ok(functions["b"])
        assert result_a["body"] == {
            "table": "table-a",
            "origin": "https://a.example",
            "utils": "a-v1",
        }
        assert result_b["body"] == {
            "table": "table-b",
            "origin": "https://b.example",
            "utils": "b-v1",
        }
        # same size and mtime as b-v1: the pyc passes its mtime-and-size check, only a
        # load from source can tell
        helper = project_cwd / "functions/b/utils.py"
        mtime_ns = helper.stat().st_mtime_ns
        helper.write_text("WHO = 'b-v2'\n")
        os.utime(helper, ns=(mtime_ns, mtime_ns))
        edited = await invoke_ok(functions["b"])
        assert edited["body"]["utils"] == "b-v2"

    return check()


@pulumi.runtime.test
def test_dev_mode_evicts_project_modules_under_an_interpreter_above_the_project(
    project_cwd, monkeypatch
):
    """A system interpreter (`/usr` over `/usr/src/app`) puts the whole project under
    `sys.prefix`; that must not shield the project's own modules from eviction."""
    monkeypatch.setattr(sys, "prefix", str(project_cwd.parent))
    write_files(
        project_cwd,
        {
            "functions/app/handler.py": (
                "import utils\n"
                "def main(event, context):\n"
                "    return {'statusCode': 200, 'body': utils.WHO}\n"
            ),
            "functions/app/utils.py": "WHO = 'v1'\n",
        },
    )
    fn = Function("fn", handler="functions/app::handler.main")
    _ = fn.resources

    async def check():
        assert (await invoke_ok(fn))["body"] == "v1"
        (project_cwd / "functions/app/utils.py").write_text("WHO = 'v2-edited'\n")
        assert (await invoke_ok(fn))["body"] == "v2-edited"

    return check()


@pulumi.runtime.test
def test_dev_mode_nested_handler_imports_from_the_folder_root(project_cwd):
    """Lambda imports `sub.handler` from the zip root, so the handler finds stlv_resources
    and the folder's packages from there, not from its own directory."""
    write_files(
        project_cwd,
        {
            "functions/nested/sub/handler.py": (
                "from stlv_resources import Resources\n"
                "from sub import helper\n"
                "def main(event, context):\n"
                "    return {'statusCode': 200,"
                " 'body': [Resources.table.table_name, helper.WHO, __name__]}\n"
            ),
            "functions/nested/sub/helper.py": "WHO = 'helper'\n",
        },
    )
    fn = Function("fn", handler="functions/nested::sub/handler.main", links=[table_link("t")])
    _ = fn.resources

    async def check():
        result = await invoke_ok(fn)
        assert result["body"] == ["t", "helper", "sub.handler"]

    return check()


@pulumi.runtime.test
def test_dev_mode_keeps_installed_packages_between_calls(project_cwd, monkeypatch):
    """The venv usually sits inside the project; its modules survive the per-call eviction
    (numpy cannot load twice), so module state persists across calls like on a warm Lambda."""
    write_files(
        project_cwd,
        {
            ".venv/pkg.py": "import itertools\nCALLS = itertools.count()\n",
            "functions/h.py": "import pkg\n" + HANDLER_OK.replace("{}", "{'n': next(pkg.CALLS)}"),
        },
    )
    monkeypatch.setattr(sys, "prefix", str(project_cwd / ".venv"))
    monkeypatch.syspath_prepend(str(project_cwd / ".venv"))
    fn = Function("fn", handler="functions/h.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn) == {"n": 0}
        assert await invoke_ok(fn) == {"n": 1}

    return check()


@pulumi.runtime.test
def test_dev_mode_single_file_handler_dataclass_under_future_annotations(project_cwd):
    """dataclasses resolve string annotations through `sys.modules[cls.__module__]`, so the
    single-file handler module must be registered under its name before it runs."""
    write_files(
        project_cwd,
        {
            "functions/dc.py": (
                "from __future__ import annotations\n"
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Item:\n    a: int\n"
                "def main(event, context):\n    return {'a': Item(1).a}\n"
            )
        },
    )
    fn = Function("fn", handler="functions/dc.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn) == {"a": 1}

    return check()


@pulumi.runtime.test
def test_dev_mode_function_without_links_has_no_resources_module(project_cwd):
    """The on-disk stlv_resources.py is the folder's union for the IDE. A Lambda without
    links or CORS ships no copy, so its import fails there; dev fails the same way instead
    of serving the sibling's file."""
    write_files(
        project_cwd,
        {
            "functions/shared/linked.py": "def main(event, context):\n    return {}\n",
            "functions/shared/plain.py": (
                "from stlv_resources import Resources\n"
                "def main(event, context):\n    return {'statusCode': 200}\n"
            ),
        },
    )
    linked = Function("linked", handler="functions/shared::linked.main", links=[table_link("t")])
    plain = Function("plain", handler="functions/shared::plain.main")
    _ = linked.resources, plain.resources
    assert (project_cwd / "functions/shared/stlv_resources.py").exists()

    async def check():
        result = await invoke(plain)
        assert type(result.error_result) is ModuleNotFoundError
        assert result.error_result.name == "stlv_resources"
        assert result.success_result is None

    return check()


@mark.parametrize(
    ("handler", "source", "error", "match"),
    [
        param(
            "functions/err::handler.main",
            "def main(:\n",
            SyntaxError,
            "invalid syntax",
            id="syntax_error",
        ),
        param(
            "functions/err::handler.main",
            "import sys\nsys.exit(3)\n",
            SystemExit,
            r"^3$",
            id="exit_at_import",
        ),
        param(
            "functions/err::handler.main",
            "import sys\ndef main(event, context):\n    sys.exit(3)\n",
            SystemExit,
            r"^3$",
            id="exit_in_handler",
        ),
        param(
            "functions/solo.main",
            "import simple2\n" + HANDLER_OK,
            ModuleNotFoundError,
            "No module named 'simple2'",
            id="single_file_sibling_import",
        ),
        param(
            "functions/json.main",
            HANDLER_OK,
            ImportError,
            "'json' is an already imported module",
            id="single_file_named_like_an_imported_module",
        ),
        param(
            "functions/err::json.main",
            HANDLER_OK,
            ImportError,
            "'json' is an already imported module",
            id="folder_file_named_like_an_imported_module",
        ),
        param(
            "functions/err::json/main.main",
            HANDLER_OK,
            ImportError,
            "imports as 'json.main' but 'json' is an already imported module",
            id="folder_package_named_like_an_imported_module",
        ),
    ],
)
@pulumi.runtime.test
def test_dev_mode_handler_failures_become_error_results(
    project_cwd, handler, source, error, match
):
    """An import-time error or `sys.exit()` is reported to the caller like any handler
    exception; the dev server keeps running. A single-file handler has nothing on sys.path,
    so importing a sibling file fails as it does on Lambda. A handler file or package named
    like a module the process already imported (`json.py`, `json/main.py`) is refused: an
    import would return that module and never read the file, on Lambda too."""
    fn = Function("fn", handler=handler)
    write_files(project_cwd, {fn.config.full_handler_python_path: source})
    _ = fn.resources

    async def check():
        result = await invoke(fn)
        assert type(result.error_result) is error
        assert re.search(match, str(result.error_result))
        assert result.success_result is None

    return check()
