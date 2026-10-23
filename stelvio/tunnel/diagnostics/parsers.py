"""Pure, allowlisted projections of bounded macOS diagnostic command output."""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

IPV4_VERSION = 4
IPV4_OCTETS = 4
MAX_PORT = 65535


@dataclass
class Parsed:
    data: dict
    recognized: bool = False
    warnings: list[dict] = field(default_factory=list)

    def warn(self, line: int, code: str = "unrecognized-record") -> None:
        # Neither source text nor exception messages belong in the public report.
        self.warnings.append({"code": code, "line": line})


def _lines(text: str) -> list[tuple[int, str]]:
    return [
        (number, line.strip()) for number, line in enumerate(text.splitlines(), 1) if line.strip()
    ]


def _flags(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _number_after(words: list[str], name: str, base: int = 10) -> int | None:
    return int(words[words.index(name) + 1], base) if name in words else None


def _address(line: str) -> dict:
    words = line.split()
    address = ipaddress.ip_address(words[1])
    if (words[0] == "inet") != (address.version == IPV4_VERSION):
        raise ValueError("Address family differs")
    value = {
        "family": "ipv4" if address.version == IPV4_VERSION else "ipv6",
        "address": str(address),
        "prefix_length": None,
        "scope_id": _number_after(words, "scopeid", 16),
        "zone": getattr(address, "scope_id", None),
        "broadcast": None,
        "peer": None,
        "flags": [
            word
            for word in words[2:]
            if word
            in {
                "autoconf",
                "temporary",
                "secured",
                "deprecated",
                "tentative",
                "duplicated",
                "detached",
                "optimistic",
            }
        ],
    }
    if "netmask" in words:
        mask = words[words.index("netmask") + 1]
        if mask.startswith("0x"):
            mask = str(ipaddress.IPv4Address(int(mask, 16)))
        value["prefix_length"] = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    elif "prefixlen" in words:
        value["prefix_length"] = ipaddress.ip_network(
            f"{str(address).split('%')[0]}/{words[words.index('prefixlen') + 1]}", strict=False
        ).prefixlen
    for marker, key in (("broadcast", "broadcast"), ("-->", "peer")):
        if marker in words:
            value[key] = str(ipaddress.ip_address(words[words.index(marker) + 1]))
    return value


def _interface_detail(value: dict, line: str) -> bool:  # noqa: PLR0911 - optional interface fields
    """Read one optional ifconfig detail without retaining arbitrary lines."""
    match = re.fullmatch(r"(options|nd6 options)=([0-9a-fA-F]+)<([^>]*)>", line)
    if match:
        key = "options" if match[1] == "options" else "nd6_options"
        value[key] = _flags(match[3])
        value[f"{key}_value"] = int(match[2], 16)
        return True
    for prefix, key in (
        ("ether ", "link_address"),
        ("lladdr ", "link_address"),
        ("media: ", "media"),
        ("status: ", "status"),
    ):
        if line.startswith(prefix):
            value[key] = line.removeprefix(prefix)
            return True
    member = re.fullmatch(r"member:\s+(\S+) flags=([0-9a-fA-F]+)<([^>]*)>", line)
    if member:
        value["bridge_members"].append(
            {"name": member[1], "flags": _flags(member[3]), "flags_value": int(member[2], 16)}
        )
        return True
    if line.startswith("ifmaxaddr ") and value["bridge_members"]:
        match = re.fullmatch(r"ifmaxaddr (\d+) port (\d+) priority (\d+) path cost (\d+)", line)
        if match:
            value["bridge_members"][-1].update(
                zip(
                    ("max_addresses", "port", "priority", "path_cost"),
                    map(int, match.groups()),
                    strict=True,
                )
            )
            return True
    if line == "Configuration:":
        return True
    # Bridge parameters are named scalars, not a copy of the Configuration block.
    matches = re.findall(
        r"(?:^|\s)(root id|id|priority|hellotime|fwddelay|maxage|holdcnt|proto|"
        r"maxaddr|timeout|ifcost|port|ipfilter|flags) (\S+)",
        line,
    )
    if matches:
        for key, item in matches:
            value["bridge_configuration"][key.replace(" ", "_")] = (
                int(item) if item.isdecimal() else item
            )
        return True
    return False


def interfaces(text: str) -> Parsed:
    result = Parsed({"interfaces": []})
    current = None
    for number, line in _lines(text):
        match = re.fullmatch(r"(\S+): flags=([0-9a-fA-F]+)<([^>]*)> mtu (\d+)(?: .*)?", line)
        if match:
            current = {
                "name": match[1],
                "flags": _flags(match[3]),
                "flags_value": int(match[2], 16),
                "mtu": int(match[4]),
                "options": [],
                "link_address": None,
                "media": None,
                "status": None,
                "addresses": [],
                "bridge_members": [],
                "bridge_configuration": {},
            }
            result.data["interfaces"].append(current)
            result.recognized = True
            continue
        # A malformed interface header must not attach its addresses to the previous one.
        if re.match(r"\S+: flags=", line):
            current = None
        try:
            if current is not None and line.startswith(("inet ", "inet6 ")):
                current["addresses"].append(_address(line))
            elif current is None or not _interface_detail(current, line):
                result.warn(number)
        except (ValueError, IndexError):
            result.warn(number, "invalid-record")
    return result


def route_network(destination: str, family: str) -> str | None:
    if destination == "default":
        return "0.0.0.0/0" if family == "ipv4" else "::/0"
    address, _, bits = destination.partition("/")
    address = address.split("%", 1)[0]
    try:
        if family == "ipv4":
            pieces = address.split(".")
            if not 1 <= len(pieces) <= IPV4_OCTETS or any(not part.isdecimal() for part in pieces):
                return None
            address = ".".join(pieces + ["0"] * (IPV4_OCTETS - len(pieces)))
            bits = bits or str(len(pieces) * 8)
        else:
            bits = bits or "128"
        network = ipaddress.ip_network(f"{address}/{bits}", strict=False)
        return str(network) if network.version == (4 if family == "ipv4" else 6) else None
    except ValueError:
        return None


def routes(text: str) -> Parsed:
    result = Parsed({"routes": []})
    family = None
    headers = []
    for number, line in _lines(text):
        if line in {"Internet:", "Internet6:"}:
            family = "ipv4" if line == "Internet:" else "ipv6"
            headers = []
        elif line.startswith("Destination"):
            headers = line.split()
            result.recognized = family is not None
        elif line == "Routing tables":
            continue
        elif family and headers:
            columns = line.split()
            try:
                fields = dict(zip(headers, columns, strict=False))
                interface = fields.get("Netif", fields.get("Iface"))
                destination = fields["Destination"]
                network = route_network(destination, family)
                if not interface or not network or len(columns) < headers.index("Flags") + 1:
                    raise ValueError("Invalid route")  # noqa: TRY301
                expiration = fields.get("Expire")
                result.data["routes"].append(
                    {
                        "family": family,
                        "destination": destination,
                        "network": network,
                        "gateway": fields["Gateway"],
                        "flags": list(fields["Flags"]),
                        "interface": interface,
                        "expires_seconds": int(expiration)
                        if expiration and expiration.isdecimal()
                        else None,
                        "expiration_marker": expiration
                        if expiration and not expiration.isdecimal()
                        else None,
                    }
                )
            except (ValueError, KeyError):
                result.warn(number, "invalid-record")
        else:
            result.warn(number)
    return result


def route_lookup(text: str) -> Parsed:
    result = Parsed(
        {
            "destination": None,
            "mask": None,
            "gateway": None,
            "interface": None,
            "flags": [],
            "metrics": {},
        }
    )
    metric_headers = []
    for number, line in _lines(text):
        key, separator, value = line.partition(":")
        if separator and key in {
            "route to",
            "destination",
            "mask",
            "gateway",
            "interface",
            "flags",
        }:
            key = "route_to" if key == "route to" else key
            result.data[key] = (
                _flags(value.strip().strip("<>")) if key == "flags" else value.strip()
            )
            result.recognized = True
        elif line.startswith("recvpipe"):
            metric_headers = line.split()
        elif metric_headers:
            try:
                result.data["metrics"] = dict(
                    zip(
                        (key.replace(",", "_") for key in metric_headers),
                        map(int, line.split()),
                        strict=True,
                    )
                )
                metric_headers = []
            except ValueError:
                result.warn(number, "invalid-record")
        else:
            result.warn(number)
    return result


def resolvers(text: str) -> Parsed:  # noqa: C901, PLR0912 - scutil's labelled resolver fields
    result = Parsed({"resolvers": []})
    scope = "global"
    current = None
    for number, line in _lines(text):
        if line == "No DNS configuration available":
            result.recognized = True
            continue
        if line.startswith("DNS configuration"):
            scope = (
                "scoped"
                if "scoped" in line
                else "supplemental"
                if "supplemental" in line
                else "global"
            )
            current = None
            result.recognized = True
            continue
        match = re.fullmatch(r"resolver #(\d+)", line)
        if match:
            current = {
                "scope": scope,
                "number": int(match[1]),
                "domain": None,
                "search_domains": [],
                "nameservers": [],
                "interface_index": None,
                "interface": None,
                "options": [],
                "flags": [],
                "reachability": None,
                "timeout": None,
                "order": None,
            }
            result.data["resolvers"].append(current)
            result.recognized = True
            continue
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or current is None:
            result.warn(number)
            continue
        try:
            if re.fullmatch(r"nameserver\[\d+\]", key):
                current["nameservers"].append(str(ipaddress.ip_address(value)))
            elif re.fullmatch(r"search domain\[\d+\]", key):
                current["search_domains"].append(value)
            elif key == "domain":
                current["domain"] = value
            elif key in {"timeout", "order", "port"}:
                current[key] = int(value)
            elif key == "if_index":
                match = re.fullmatch(r"(\d+)(?: \(([^)]+)\))?", value)
                if not match:
                    raise ValueError("Invalid interface index")  # noqa: TRY301
                current["interface_index"] = int(match[1])
                current["interface"] = match[2]
            elif key in {"options", "flags"}:
                current[key] = value.split() if key == "options" else _flags(value)
            elif key == "reach":
                match = re.fullmatch(r"(0x[0-9a-fA-F]+)(?: \((.*)\))?", value)
                if not match:
                    raise ValueError("Invalid reachability")  # noqa: TRY301
                current["reachability"] = {
                    "value": int(match[1], 16),
                    "flags": _flags(match[2] or ""),
                }
            else:
                result.warn(number)
        except ValueError:
            result.warn(number, "invalid-record")
    return result


def network_services(text: str) -> Parsed:
    result = Parsed({"services": []})
    for number, line in _lines(text):
        if line == "An asterisk (*) denotes that a network service is disabled.":
            result.recognized = True
        elif result.recognized:
            result.data["services"].append(
                {"name": line.removeprefix("*"), "enabled": not line.startswith("*")}
            )
        else:
            result.warn(number)
    return result


def service_dns(text: str) -> Parsed:
    result = Parsed({"nameservers": [], "configured": None})
    for number, line in _lines(text):
        if re.fullmatch(r"There aren't any DNS Servers set on .+\.", line):
            result.data["configured"] = False
            result.recognized = True
            continue
        try:
            result.data["nameservers"].append(str(ipaddress.ip_address(line)))
            result.data["configured"] = True
            result.recognized = True
        except ValueError:
            result.warn(number, "invalid-record")
    return result


def proxies(text: str) -> Parsed:
    result = Parsed({"settings": {}, "pac": {"configured": None, "scheme": None}})
    enables = {
        "HTTPEnable",
        "HTTPSEnable",
        "FTPEnable",
        "SOCKSEnable",
        "ProxyAutoConfigEnable",
        "ProxyAutoDiscoveryEnable",
        "ExcludeSimpleHostnames",
    }
    ports = {"HTTPPort", "HTTPSPort", "FTPPort", "SOCKSPort"}
    hosts = {"HTTPProxy", "HTTPSProxy", "FTPProxy", "SOCKSProxy"}
    for number, line in _lines(text):
        if line == "<dictionary> {":
            result.recognized = True
        key, separator, value = line.partition(" : ")
        if not separator:
            continue
        try:
            if key in enables and value in {"0", "1"}:
                result.data["settings"][key] = value == "1"
            elif key in ports and value.isdecimal() and 0 <= int(value) <= MAX_PORT:
                result.data["settings"][key] = int(value)
            elif key in hosts and re.fullmatch(r"[a-zA-Z0-9.:[\]-]{1,253}", value):
                result.data["settings"][key] = value
            elif key == "ProxyAutoConfigURLString":
                scheme = urlsplit(value).scheme.lower()
                result.data["pac"] = {
                    "configured": bool(value),
                    "scheme": scheme if scheme in {"http", "https", "file"} else None,
                }
            elif key in enables | ports | hosts:
                result.warn(number, "invalid-record")
        except ValueError:
            result.warn(number, "invalid-record")
    return result


def macos_build(text: str) -> Parsed:
    result = Parsed({"product_name": None, "product_version": None, "build_version": None})
    keys = {
        "ProductName": "product_name",
        "ProductVersion": "product_version",
        "BuildVersion": "build_version",
    }
    for number, line in _lines(text):
        key, separator, value = line.partition(":")
        if separator and key in keys and value.strip():
            result.data[keys[key]] = value.strip()
            result.recognized = True
        else:
            result.warn(number)
    return result


def firewall(text: str) -> Parsed:
    result = Parsed({"enabled": None, "state": None})
    for number, line in _lines(text):
        match = re.fullmatch(r"Firewall is (enabled|disabled)\. \(State = (\d+)\)", line)
        if match:
            result.data.update(enabled=match[1] == "enabled", state=int(match[2]))
            result.recognized = True
        else:
            result.warn(number)
    return result


def packet_filter(text: str) -> Parsed:
    result = Parsed({"enabled": None, "running_seconds": None, "statistics": []})
    group = None
    for number, line in _lines(text):
        match = re.fullmatch(
            r"Status: (Enabled|Disabled)(?: for (?:(\d+) days? )?(\d+):(\d+):(\d+))?"
            r"(?:\s+Debug: (\w+))?",
            line,
        )
        if match:
            duration = (
                (
                    int(match[2] or 0) * 86400
                    + int(match[3]) * 3600
                    + int(match[4]) * 60
                    + int(match[5])
                )
                if match[3]
                else None
            )
            result.data.update(enabled=match[1] == "Enabled", running_seconds=duration)
            result.recognized = True
        elif re.fullmatch(r"[A-Za-z][A-Za-z -]*", line):
            group = re.sub(r"\s+Total\s+Rate$", "", line).replace(" ", "_").lower()
        else:
            metric = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9 _-]*?)\s+(\d+)(?:\s+([0-9.]+)/s)?", line)
            if metric:
                try:
                    rate = float(metric[3]) if metric[3] else None
                    count = int(metric[2])
                    if rate is not None and not math.isfinite(rate):
                        raise ValueError("Invalid rate")  # noqa: TRY301
                except ValueError:
                    result.warn(number, "invalid-record")
                    continue
                result.data["statistics"].append(
                    {
                        "group": group,
                        "name": metric[1].strip().replace(" ", "_"),
                        "value": count,
                        "per_second": rate,
                    }
                )
            else:
                result.warn(number)
    return result


def helper_service(text: str) -> Parsed:
    result = Parsed(
        {
            "state": None,
            "pid": None,
            "runs": None,
            "last_exit_code": None,
            "type": None,
            "program": None,
        }
    )
    depth = 0
    for number, line in _lines(text):
        # Only direct children of the service block are public. Nested environment,
        # sockets and arguments must not be mistaken for service status fields.
        key, separator, value = line.partition(" = ")
        if (
            depth == 1
            and separator
            and key in {"state", "pid", "runs", "last exit code", "type", "program"}
        ):
            name = key.replace(" ", "_")
            try:
                result.data[name] = (
                    (None if value == "(never exited)" else int(value))
                    if name in {"pid", "runs", "last_exit_code"}
                    else value
                )
                result.recognized = True
            except ValueError:
                result.warn(number, "invalid-record")
        if line.endswith("{"):
            depth += 1
        elif line == "}":
            depth = max(0, depth - 1)
    return result


def resolver_file(text: str) -> Parsed:
    result = Parsed(
        {"nameservers": [], "port": None, "domain": None, "search_order": None, "timeout": None},
        recognized=not text.strip(),
    )
    for number, line in _lines(text):
        if line.startswith("#"):
            result.recognized = True
            continue
        key, separator, value = line.partition(" ")
        try:
            if separator and key == "nameserver":
                result.data["nameservers"].append(str(ipaddress.ip_address(value)))
            elif separator and key in {"port", "search_order", "timeout"}:
                result.data[key] = int(value)
            elif separator and key == "domain" and re.fullmatch(r"[a-zA-Z0-9.-]+", value):
                result.data[key] = value
            else:
                result.warn(number)
                continue
            result.recognized = True
        except ValueError:
            result.warn(number, "invalid-record")
    return result


PARSERS = {
    "interfaces": interfaces,
    "routes": routes,
    "default_route": route_lookup,
    "selected_route": route_lookup,
    "resolvers": resolvers,
    "network_services": network_services,
    "service_dns": service_dns,
    "proxies": proxies,
    "macos_build": macos_build,
    "firewall": firewall,
    "packet_filter": packet_filter,
    "helper_service": helper_service,
}


def parse_observation(name: str, value: dict) -> dict:
    """Consume raw command output, retaining metadata even when parsing fails."""
    source = value.get("data")
    if source is None:
        return value
    data = {"exit_code": source["exit_code"], "truncated": source["truncated"]}
    result = dict(value, data=data)
    if value["status"] != "collected":
        return result
    text = source["output"]
    # An incomplete final line may otherwise turn a cut-off address into a valid one.
    if source["truncated"] and not text.endswith("\n"):
        text = text.rpartition("\n")[0]
    try:
        parsed = PARSERS[name](text)
    except (ValueError, IndexError, KeyError, OverflowError):
        result.update(status="failed", error={"code": "parse-failed"})
        return result
    data.update(
        parsed.data,
        parse_warnings=parsed.warnings,
        incomplete=bool(parsed.warnings or source["truncated"]),
    )
    if not parsed.recognized:
        data["incomplete"] = True
        result.update(status="failed", error={"code": "parse-unrecognized"})
    elif parsed.warnings:
        result["error"] = {"code": "partial-parse"}
    return result
