"""Structured system observations and complete literal rendering."""

import io
import json
import time

import pytest
from click.testing import CliRunner
from rich.console import Console

from stelvio.cli.tunnel import tunnel
from stelvio.tunnel.diagnostics import parsers, protocol, report

from .diagnostics_fixtures import RESOLVER_FILE, SYSTEM_OUTPUTS


def observation(text, *, exit_code=0, truncated=False):
    return protocol.observation(
        {"output": text, "exit_code": exit_code, "truncated": truncated},
        status="collected" if exit_code == 0 or truncated else "unavailable",
        code=None if exit_code == 0 or truncated else "command-denied-or-failed",
    )


def assert_no_output(value):
    if isinstance(value, dict):
        assert "output" not in value
        for item in value.values():
            assert_no_output(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_output(item)


@pytest.mark.parametrize("name", SYSTEM_OUTPUTS)
def test_each_command_sample_has_a_structured_projection(name):
    value = parsers.parse_observation(name, observation(SYSTEM_OUTPUTS[name]))
    assert value["status"] == "collected", value
    assert value["error"] is None, value
    assert value["data"]["incomplete"] is False
    assert value["data"]["exit_code"] == 0
    assert value["data"]["truncated"] is False
    assert_no_output(value)
    assert "secret" not in json.dumps(value)


def test_interfaces_preserve_addresses_scopes_peers_and_bridge_configuration():
    loopback, ethernet, bridge, tunnel_interface = parsers.interfaces(
        SYSTEM_OUTPUTS["interfaces"]
    ).data["interfaces"]
    assert len(loopback["addresses"]) == 3
    assert ethernet["mtu"] == 1500
    assert "UP" in ethernet["flags"]
    assert ethernet["link_address"] == "02:00:00:00:00:01"
    assert ethernet["status"] == "active"
    assert ethernet["addresses"][0]["prefix_length"] == 24
    assert ethernet["addresses"][0]["broadcast"] == "192.0.2.255"
    link_local = ethernet["addresses"][1]
    assert link_local["address"] == "fe80::1%en0"
    assert link_local["zone"] == "en0"
    assert link_local["scope_id"] == 14
    assert link_local["prefix_length"] == 64
    assert ethernet["addresses"][2]["flags"] == ["autoconf", "temporary"]
    assert bridge["bridge_members"][0]["name"] == "en1"
    assert bridge["bridge_members"][0]["port"] == 10
    assert bridge["bridge_configuration"]["proto"] == "stp"
    assert bridge["bridge_configuration"]["timeout"] == 1200
    assert tunnel_interface["addresses"][0]["peer"] == "172.16.0.2"
    assert tunnel_interface["addresses"][0]["prefix_length"] == 32


def test_routes_handle_both_families_abbreviations_and_variable_columns():
    value = parsers.routes(SYSTEM_OUTPUTS["routes"]).data["routes"]
    assert value[0]["network"] == "0.0.0.0/0"
    assert value[1]["network"] == "192.0.2.0/23"
    assert value[2]["network"] == "10.254.0.0/16"
    assert value[3]["expires_seconds"] == 1153
    assert value[4]["expiration_marker"] == "!"
    assert value[5]["network"] == "::/0"
    assert value[6]["destination"] == "fe80::%utun7/64"
    assert value[6]["network"] == "fe80::/64"
    extra_columns = SYSTEM_OUTPUTS["routes"].replace("Netif Expire", "Refs Use Netif Expire")
    extra_columns = extra_columns.replace("UGScg      en0", "UGScg 0 10 en0")
    result = parsers.routes(extra_columns)
    assert result.data["routes"][0]["interface"] == "en0"
    assert result.warnings  # The other rows do not match the expanded header.


def test_route_lookup_metrics_are_numbers():
    value = parsers.route_lookup(SYSTEM_OUTPUTS["selected_route"]).data
    assert value["gateway"] == "192.0.2.2"
    assert value["interface"] == "utun8"
    assert value["metrics"]["mtu"] == 1300
    assert value["metrics"]["rtt_msec"] == 1
    assert value["flags"] == ["UP", "GATEWAY", "DONE", "STATIC"]


def test_resolver_scopes_and_nameservers_remain_distinct():
    first, second, scoped = parsers.resolvers(SYSTEM_OUTPUTS["resolvers"]).data["resolvers"]
    assert first["search_domains"] == ["example.test", "internal.test"]
    assert first["nameservers"] == ["192.0.2.53", "2001:db8::53"]
    assert first["reachability"]["value"] == 0x30002
    assert second["options"] == ["mdns"]
    assert second["order"] == 300000
    assert scoped["scope"] == "scoped"
    assert scoped["number"] == first["number"] == 1
    assert scoped["interface"] == "en0"
    assert scoped["interface_index"] == 14
    assert scoped["nameservers"] == ["fe80::53%en0"]


def test_services_disabled_state_and_explicit_empty_dns():
    value = parsers.network_services(SYSTEM_OUTPUTS["network_services"]).data
    assert value["services"][-1] == {"name": "Disabled Service", "enabled": False}
    value = parsers.parse_observation(
        "service_dns", observation("There aren't any DNS Servers set on Wi-Fi.\n")
    )
    assert value["status"] == "collected"
    assert value["data"]["nameservers"] == []
    assert value["data"]["configured"] is False


def test_launchd_nesting_and_proxy_allowlist_exclude_secrets():
    value = parsers.helper_service(SYSTEM_OUTPUTS["helper_service"]).data
    assert value["type"] == "LaunchDaemon"
    assert value["state"] == "running"
    assert value["pid"] == 42
    assert value["last_exit_code"] is None
    exited = SYSTEM_OUTPUTS["helper_service"].replace("(never exited)", "-9")
    assert parsers.helper_service(exited).data["last_exit_code"] == -9
    value = parsers.proxies(SYSTEM_OUTPUTS["proxies"]).data
    assert value["settings"]["HTTPEnable"] is True
    assert value["settings"]["HTTPSEnable"] is False
    assert value["settings"]["HTTPPort"] == 8080
    assert value["pac"] == {"configured": True, "scheme": "https"}
    value = parsers.parse_observation(
        "proxies",
        observation("<dictionary> {\n HTTPProxy : user:secret@host\n HTTPPort : secret\n}\n"),
    )
    assert value["data"]["settings"] == {}
    assert "secret" not in json.dumps(value)
    assert value["data"]["incomplete"] is True


def test_packet_filter_and_resolver_file_types():
    value = parsers.packet_filter(SYSTEM_OUTPUTS["packet_filter"]).data
    assert value["enabled"] is True
    assert value["running_seconds"] == 93784
    assert value["statistics"][0]["value"] == 12
    assert value["statistics"][0]["group"] == "state_table"
    assert value["statistics"][1]["per_second"] == 1.5
    value = parsers.resolver_file(RESOLVER_FILE)
    assert value.warnings == []
    assert value.data == {
        "nameservers": ["192.0.2.53", "2001:db8::53"],
        "port": 5353,
        "domain": "example.test",
        "search_order": 100,
        "timeout": 2,
    }
    assert parsers.resolver_file("nameserver secret\nport wrong\n").warnings == [
        {"code": "invalid-record", "line": 1},
        {"code": "invalid-record", "line": 2},
    ]


def test_packet_filter_malformed_rates_do_not_discard_other_counters():
    text = (
        SYSTEM_OUTPUTS["packet_filter"]
        + " bad-rate 1 1.2.3/s\n overflow 2 "
        + "9" * 400
        + "/s\n healthy 3 0.2/s\n"
    )
    value = parsers.parse_observation("packet_filter", observation(text))
    assert value["status"] == "collected"
    assert value["data"]["statistics"][-1]["name"] == "healthy"
    assert len(value["data"]["parse_warnings"]) == 2
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("proxies", "<dictionary> {\n}\n"),
        ("resolvers", "No DNS configuration available\n"),
        ("routes", "Routing tables\nInternet:\nDestination Gateway Flags Netif Expire\n"),
        ("network_services", "An asterisk (*) denotes that a network service is disabled.\n"),
    ],
)
def test_explicit_empty_configurations_are_successful(name, text):
    value = parsers.parse_observation(name, observation(text))
    assert value["status"] == "collected"
    assert value["error"] is None
    assert value["data"]["parse_warnings"] == []


def test_resolver_file_collection_uses_public_receipts_and_parsed_configuration(
    tmp_path, monkeypatch
):
    name = "stelvio." + "a" * 32 + "." + "b" * 8 + "." + "c" * 16 + ".00"
    (tmp_path / name).write_text(RESOLVER_FILE)
    monkeypatch.setattr(report, "Path", lambda _: tmp_path)
    sections = {
        "helper": {
            "data": {
                "journal_units": [{"resolver_receipts": [{"name": name}, {"name": "../secret"}]}]
            }
        }
    }
    value = report._resolver_files(sections)
    assert len(value["data"]["files"]) == 1
    item = value["data"]["files"][0]
    assert item["name"] == name
    assert item["configuration"]["port"] == 5353
    assert item["parse_warnings"] == []
    assert item["error"] is None
    assert_no_output(value)
    # A syntactically valid but cut-off final value must not become configuration.
    (tmp_path / name).write_text("#" + "x" * (report.RESOLVER_LIMIT - 9) + "\nport 5353")
    item = report._resolver_files(sections)["data"]["files"][0]
    assert item["truncated"] is True
    assert item["incomplete"] is True
    assert item["configuration"]["port"] is None


@pytest.mark.parametrize("name", SYSTEM_OUTPUTS)
def test_unrecognized_success_never_silently_becomes_an_empty_success(name):
    value = parsers.parse_observation(name, observation("unrecognized secret\n"))
    assert value["status"] == "failed"
    assert value["error"]["code"] == "parse-unrecognized"
    assert "secret" not in json.dumps(value)
    assert_no_output(value)


def test_partial_and_truncated_output_keep_complete_records():
    value = parsers.parse_observation(
        "service_dns", observation("192.0.2.53\nsecret\n2001:db8::53\n")
    )
    assert value["data"]["nameservers"] == ["192.0.2.53", "2001:db8::53"]
    assert value["data"]["parse_warnings"] == [{"code": "invalid-record", "line": 2}]
    assert value["error"] == {"code": "partial-parse"}
    value = parsers.parse_observation(
        "service_dns", observation("192.0.2.53\n192.0.2.5", exit_code=-9, truncated=True)
    )
    assert value["data"]["nameservers"] == ["192.0.2.53"]
    assert value["data"]["incomplete"] is True
    assert value["data"]["truncated"] is True
    assert value["data"]["exit_code"] == -9


def test_malformed_interface_does_not_inherit_previous_interface():
    text = (
        "lo0: flags=8049<UP> mtu 1000\nother: flags=broken\n inet 192.0.2.1 netmask 0xffffffff\n"
    )
    result = parsers.interfaces(text)
    assert result.data["interfaces"][0]["addresses"] == []
    assert len(result.warnings) == 2


@pytest.mark.parametrize("code", ["command-timeout", "report-deadline"])
def test_command_failure_is_preserved_separately_from_parsing(code):
    source = protocol.observation(status="failed", code=code)
    assert parsers.parse_observation("interfaces", source) == source
    denied = parsers.parse_observation("packet_filter", observation("secret\n", exit_code=1))
    assert denied["status"] == "unavailable"
    assert denied["error"]["code"] == "command-denied-or-failed"
    assert denied["data"] == {"exit_code": 1, "truncated": False}


def test_collection_parses_nested_commands_and_isolates_parse_failure(monkeypatch):
    monkeypatch.setattr(report.sys, "platform", "darwin")
    monkeypatch.setattr(report, "artifacts", lambda: protocol.observation({}))
    monkeypatch.setattr(report, "helper", lambda: protocol.observation({}))
    monkeypatch.setattr(report, "_resolver_files", lambda _: protocol.observation({"files": []}))
    monkeypatch.setattr(report, "_processes", lambda _: protocol.observation({}))
    monkeypatch.setattr(
        report,
        "sessions",
        lambda: protocol.observation(
            {
                "sessions": [
                    {
                        "session": {
                            "session_id": "session",
                            "vpcs": [{"identity": "vpc", "ranges": ["10.254.0.0/16"]}],
                        }
                    }
                ]
            }
        ),
    )
    names = {tuple(arguments): name for name, arguments in report.SYSTEM_COMMANDS.items()}
    calls = []

    def command(arguments, deadline):
        calls.append(arguments)
        assert deadline > time.monotonic()
        name = names.get(tuple(arguments))
        if name == "macos_build":
            return observation("unrecognized secret\n")
        if name:
            return observation(SYSTEM_OUTPUTS[name])
        if arguments[0] == "/sbin/route":
            return observation(SYSTEM_OUTPUTS["selected_route"])
        assert arguments[:2] == ["/usr/sbin/networksetup", "-getdnsservers"]
        return observation(SYSTEM_OUTPUTS["service_dns"])

    monkeypatch.setattr(report, "command", command)
    value = report.collect()
    assert value["schema_version"] == 2
    assert value["sections"]["macos_build"]["status"] == "failed"
    assert value["sections"]["interfaces"]["status"] == "collected"
    lookup = value["sections"]["selected_routes"]["data"]["lookups"][0]
    assert lookup["session"] == "session"
    assert lookup["observation"]["data"]["interface"] == "utun8"
    dns = value["sections"]["network_service_dns"]["data"]["services"]
    assert len(dns) == 2
    assert dns[0]["dns"]["data"]["nameservers"] == ["192.0.2.53", "2001:db8::53"]
    assert not any("Disabled Service" in arguments for arguments in calls)
    assert_no_output(value)
    assert "secret" not in json.dumps(value)
    monkeypatch.setattr(report, "collect", lambda: value)
    result = CliRunner().invoke(tunnel, ["diagnose", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == value


@pytest.mark.parametrize("width", [40, 100, 180])
def test_tables_render_every_row_and_nested_data_without_python_representations(width):
    value = {
        "generated_at": "now",
        "duration_seconds": 0.1,
        "truncated": False,
        "sections": {
            "interfaces": protocol.observation(
                parsers.interfaces(SYSTEM_OUTPUTS["interfaces"]).data
            ),
            "routes": protocol.observation(
                {
                    "routes": [
                        {"destination": f"row{index:03}", "interface": "utun8"}
                        for index in range(75)
                    ]
                }
            ),
            "nested": protocol.observation(
                {
                    "units": [{"unit": "unit-one", "dns": {"nameservers": ["192.0.2.53"]}}],
                    "literal": "[bold]literal[/bold]",
                }
            ),
        },
        "findings": [{"code": "route-conflict", "interface": "utun7"}],
    }
    output = io.StringIO()
    report.render(value, Console(file=output, width=width, color_system=None))
    rendered = output.getvalue()
    for index in range(75):
        assert f"row{index:03}" in rendered
    assert "Addresses" in rendered
    assert "Nameservers" in rendered
    assert "unit-one" in rendered
    assert "'unit':" not in rendered
    assert "{'" not in rendered
    assert "[bold]literal[/bold]" in rendered
    assert "…" not in rendered
