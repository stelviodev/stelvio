"""Host evidence comparison must preserve meaningful DNS changes."""

from pytest import mark

from tests.integration.macos_host import stable_dns

DNS = """DNS configuration
resolver #1
  search domain[0] : example.test
  nameserver[0] : 192.0.2.53
  if_index : 14 (en0)
  port : 53
  flags : Scoped, Request A records
  reach : 0x00000002 (Reachable)
  order : 200000
resolver #2
  domain : private.test
  nameserver[0] : 127.0.0.1
  port : 61234
  options : timeout:1
"""


def test_dns_evidence_ignores_numbering_and_reachability_only():
    changed = DNS.replace("resolver #1", "resolver #8").replace(
        "0x00000002 (Reachable)", "0x00000000 (Not Reachable)"
    )
    assert stable_dns(changed) == stable_dns(DNS)


@mark.parametrize(
    ("old", "new"),
    [
        ("private.test", "foreign.test"),
        ("192.0.2.53", "192.0.2.54"),
        ("61234", "61235"),
        ("en0", "utun9"),
        ("timeout:1", "timeout:2"),
        ("200000", "200001"),
        ("Scoped", "Supplemental"),
        ("example.test", "another.test"),
    ],
)
def test_dns_evidence_detects_configuration_changes(old, new):
    assert stable_dns(DNS.replace(old, new)) != stable_dns(DNS)


def test_dns_evidence_detects_scope_removal_and_reordering():
    header, first, second = DNS.split("resolver #")
    assert stable_dns(header + "resolver #" + second + "resolver #" + first) != stable_dns(DNS)
    assert stable_dns(header + "resolver #" + first) != stable_dns(DNS)
