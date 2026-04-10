from typing import Any

from pulumi import Input, export

from stelvio.context import context


def export_output(key: str, value: Input[Any]) -> None:
    """Export a value as a Pulumi stack output.

    Use this in your ``stlv_app.py`` to expose values after deploy::

        from stelvio import export_output

        api = Api("my-api")
        export_output("api_url", api.resources.stage.invoke_url)
    """
    export(key, value)


__all__ = ["context", "export_output"]
