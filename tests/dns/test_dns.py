"""Behavioural tests for DNS helper capability detection."""

from unittest.mock import patch

from pulumi import ResourceOptions

from stelvio.dns import Record, _call_with_optional_resource_options


class _SentinelRecord(Record):
    def __init__(self):
        # Bypass Resource requirement — helper only needs a return value.
        self._pulumi_resource = None


def test_helper_passes_opts_when_parameter_present():
    captured: dict[str, object] = {}

    def method(*, name: str, opts: ResourceOptions | None = None) -> Record:
        captured["name"] = name
        captured["opts"] = opts
        return _SentinelRecord()

    opts = ResourceOptions()
    result = _call_with_optional_resource_options(method, name="api.example.com", opts=opts)

    assert isinstance(result, Record)
    assert captured["name"] == "api.example.com"
    assert captured["opts"] is opts


def test_helper_passes_opts_when_kwargs_accepted():
    captured: dict[str, object] = {}

    def method(*, name: str, **kwargs: object) -> Record:
        captured["name"] = name
        captured["kwargs"] = kwargs
        return _SentinelRecord()

    opts = ResourceOptions()
    _call_with_optional_resource_options(method, name="api.example.com", opts=opts)

    assert captured["name"] == "api.example.com"
    assert captured["kwargs"] == {"opts": opts}


def test_helper_omits_opts_for_legacy_signature():
    captured: dict[str, object] = {}

    def method(*, name: str) -> Record:
        captured["name"] = name
        return _SentinelRecord()

    # Would raise TypeError if helper passed opts=
    result = _call_with_optional_resource_options(
        method, name="api.example.com", opts=ResourceOptions()
    )

    assert isinstance(result, Record)
    assert captured == {"name": "api.example.com"}


def test_helper_falls_back_when_signature_raises_type_error():
    captured: list[dict[str, object]] = []

    def method(*, name: str) -> Record:
        captured.append({"name": name})
        return _SentinelRecord()

    with patch("stelvio.dns.signature", side_effect=TypeError("no signature")):
        result = _call_with_optional_resource_options(
            method, name="api.example.com", opts=ResourceOptions()
        )

    assert isinstance(result, Record)
    assert captured == [{"name": "api.example.com"}]


def test_helper_falls_back_when_signature_raises_value_error():
    captured: list[dict[str, object]] = []

    def method(*, name: str) -> Record:
        captured.append({"name": name})
        return _SentinelRecord()

    with patch("stelvio.dns.signature", side_effect=ValueError("bad signature")):
        result = _call_with_optional_resource_options(
            method, name="api.example.com", opts=ResourceOptions()
        )

    assert isinstance(result, Record)
    assert captured == [{"name": "api.example.com"}]
