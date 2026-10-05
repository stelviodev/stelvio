"""Normalized access policy and DNS names shared by components and runtime."""

import re
from enum import StrEnum
from ipaddress import ip_address


class BastionPolicy(StrEnum):
    TEMPORARY = "temporary"
    PERSISTENT = "persistent"
    DISABLED = "disabled"


class ConnectionState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPING = "stopping"
    CLEANUP_FAILED = "cleanup_failed"
    STOPPED = "stopped"


LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_DNS_NAME = 253


def normalize_dns_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("DNS names must be strings")
    name = value.removesuffix(".").lower()
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("Invalid DNS name") from error
    if (
        not name
        or len(name) > MAX_DNS_NAME
        or not all(LABEL.fullmatch(s) for s in name.split("."))
    ):
        raise ValueError("Invalid DNS name: require DNS labels without wildcards or whitespace")
    try:
        ip_address(name)
    except ValueError:
        return name
    raise ValueError("DNS names must not be IP addresses")


def normalize_dns_domains(value: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError("'dns_domains' must be a tuple or list of DNS names")
    return tuple(dict.fromkeys(normalize_dns_name(domain) for domain in value))


def domain_contains(domain: str, name: str) -> bool:
    return name == domain or name.endswith("." + domain)
