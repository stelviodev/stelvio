"""Bounded Linux host observations, independent of helper mutation APIs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from stelvio.tunnel.diagnostics.protocol import observation

if TYPE_CHECKING:
    from collections.abc import Callable

PROCESS_FIELDS = ("pid", "parent", "uid", "command")
COMMANDS = {
    "interfaces": ["/usr/sbin/ip", "-j", "address", "show"],
    "routes": ["/usr/sbin/ip", "-j", "-4", "route", "show", "table", "all"],
    "resolvers": [
        "/usr/bin/busctl",
        "--json=short",
        "get-property",
        "org.freedesktop.resolve1",
        "/org/freedesktop/resolve1",
        "org.freedesktop.resolve1.Manager",
        "DNSEx",
    ],
    "helper_service": [
        "/usr/bin/systemctl",
        "show",
        "stelvio-traforo.service",
        "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath",
    ],
    "processes": ["/bin/ps", "-axo", "pid=,ppid=,uid=,comm="],
}


def collect(name: str, command: Callable, deadline: float) -> dict:
    result = command(COMMANDS[name], deadline)
    if result.get("status") != "collected":
        return result
    raw = result["data"]["output"]
    truncated = result["data"].get("truncated", False)
    try:
        if name in {"interfaces", "routes", "resolvers"}:
            data = json.loads(raw)
            if name == "interfaces":
                data = {"interfaces": data}
            elif name == "routes":
                data = {
                    "routes": [
                        {**row, "destination": row.get("dst"), "interface": row.get("dev")}
                        for row in data
                    ]
                }
        elif name == "helper_service":
            data = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        else:
            data = {"processes": []}
            for line in raw.splitlines():
                fields = line.split(maxsplit=3)
                if len(fields) == len(PROCESS_FIELDS):
                    data["processes"].append(dict(zip(PROCESS_FIELDS, fields, strict=True)))
    except (ValueError, TypeError, KeyError):
        return observation(status="failed", code="linux-observation-invalid")
    result = observation(data)
    result["incomplete"] = truncated
    return result


def artifacts(inspect: Callable) -> dict:
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from stelvio.pulumi import get_stelvio_config_dir  # noqa: PLC0415
    from stelvio.tunnel.assets import (  # noqa: PLC0415
        DEVELOPMENT_BINARY_ENV,
        TRAFORO_VERSION,
        _architecture,
        _validate_developer_parent,
    )
    from stelvio.tunnel.linux_platform import INSTALLED_HELPER  # noqa: PLC0415
    from stelvio.tunnel.linux_plugin import TARGETS  # noqa: PLC0415
    from stelvio.tunnel.plugin import PLUGIN_VERSION  # noqa: PLC0415

    architecture = _architecture()
    value = {"pinned_traforo_version": TRAFORO_VERSION, "pinned_plugin_version": PLUGIN_VERSION}
    checksum = None
    developer = os.environ.get(DEVELOPMENT_BINARY_ENV)
    if developer:
        try:
            path = Path(developer)
            path = _validate_developer_parent(path) / path.name
            value["selected_traforo"] = inspect(path, None, os.geteuid(), architecture)
            checksum = value["selected_traforo"]["data"]["digest"]
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            value["selected_traforo"] = observation(status="failed", code=type(error).__name__)
    else:
        value["selected_traforo"] = observation(
            status="unavailable", code="linux-release-unpublished"
        )
    cache = get_stelvio_config_dir() / "bin"
    for name, path, expected, uid in (
        ("installed_traforo", INSTALLED_HELPER, checksum, 0),
        (
            "plugin",
            cache / f"session-manager-plugin-{PLUGIN_VERSION}-linux-{architecture}",
            TARGETS[architecture][2],
            os.geteuid(),
        ),
    ):
        try:
            value[name] = inspect(path, expected, uid, architecture)
        except (OSError, ValueError, RuntimeError) as error:
            value[name] = observation(status="unavailable", code=type(error).__name__)
    return observation(value)
