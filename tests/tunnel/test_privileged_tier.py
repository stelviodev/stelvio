"""Collection safety: ordinary test runs cannot opt themselves into host mutation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.integration import conftest as tiers


def config(**options):
    values = {
        "--integration-macos-privileged": False,
        "--integration-tunnel": False,
        "--integration": False,
        "--integration-vpc": False,
        "--integration-cf": False,
        "--integration-dns": False,
        "numprocesses": 0,
    }
    values.update(options)
    return SimpleNamespace(getoption=lambda key, default=None: values.get(key, default))


def item(*markers):
    node = Mock()
    node.get_closest_marker.side_effect = lambda name: name in markers
    return node


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(tiers.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tiers.platform, "mac_ver", lambda: ("15.8", (), "arm64"))
    monkeypatch.setattr(tiers.os, "geteuid", lambda: 502)


def test_default_and_regular_runner_skip_all_host_mutations():
    for selected in ({}, {"--integration": True}, {"--integration-vpc": True}):
        nodes = [
            item("integration_macos_privileged"),
            item("integration_macos_privileged", "integration_tunnel"),
        ]
        tiers.pytest_collection_modifyitems(config(**selected), nodes)
        for node in nodes:
            assert (
                node.add_marker.call_args.args[0].kwargs["reason"]
                == "need --integration-macos-privileged"
            )


def test_native_lane_does_not_implicitly_enable_aws(mac):
    host, aws, ordinary = (
        item("integration_macos_privileged"),
        item("integration_macos_privileged", "integration_tunnel"),
        item("integration"),
    )
    tiers.pytest_collection_modifyitems(
        config(**{"--integration-macos-privileged": True}), [host, aws, ordinary]
    )
    host.add_marker.assert_not_called()
    assert (
        aws.add_marker.call_args.args[0].kwargs["reason"]
        == "also need --integration-tunnel for AWS"
    )
    assert (
        ordinary.add_marker.call_args.args[0].kwargs["reason"] == "exclusive privileged macOS lane"
    )


def test_aws_requires_both_flags(mac):
    with pytest.raises(pytest.UsageError, match="also requires"):
        tiers.pytest_collection_modifyitems(config(**{"--integration-tunnel": True}), [])
    aws = item("integration_macos_privileged", "integration_tunnel")
    tiers.pytest_collection_modifyitems(
        config(**{"--integration-tunnel": True, "--integration-macos-privileged": True}), [aws]
    )
    aws.add_marker.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {"numprocesses": 2},
        {"--integration": True},
        {"--integration-vpc": True},
        {"--integration-cf": True},
        {"--integration-dns": True},
    ],
)
def test_host_lane_rejects_parallelism_and_other_tiers(mac, options):
    with pytest.raises(pytest.UsageError, match="requires -n 0"):
        tiers.pytest_collection_modifyitems(
            config(**{"--integration-macos-privileged": True, **options}), []
        )


@pytest.mark.parametrize(
    ("system", "version", "uid"),
    [("Linux", "15.8", 502), ("Darwin", "14.8", 502), ("Darwin", "15.8", 0)],
)
def test_host_lane_rejects_unsupported_or_root_execution(mac, monkeypatch, system, version, uid):
    monkeypatch.setattr(tiers.platform, "system", lambda: system)
    monkeypatch.setattr(tiers.platform, "mac_ver", lambda: (version, (), ""))
    monkeypatch.setattr(tiers.os, "geteuid", lambda: uid)
    with pytest.raises(pytest.UsageError):
        tiers.pytest_collection_modifyitems(config(**{"--integration-macos-privileged": True}), [])
