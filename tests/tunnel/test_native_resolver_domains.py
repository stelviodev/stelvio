"""Foreign resolver domains use explicit directives and label-boundary conflicts."""

import subprocess
import sys

from pytest import fixture, mark

pytestmark = mark.skipif(sys.platform != "darwin", reason="Native SystemConfiguration inventory")


@fixture(scope="module")
def parser(traforo_checker):
    return traforo_checker("resolver")


@mark.parametrize(
    ("name", "contents", "expected"),
    [
        ("EXAMPLE.Internal.", b"nameserver 127.0.0.1\n", b"example.internal\n"),
        (
            "arbitrary-name",
            b"# domain ignored.internal\n  domain DB.Example.Internal. # note\nport 5300\n",
            b"db.example.internal\n",
        ),
        ("arbitrary-name", b"domain\tdb.internal\r\nnameserver 127.0.0.1", b"db.internal\n"),
        ("fallback.internal", b"domain-name unrelated\n", b"fallback.internal\n"),
        ("fallback.internal", b"domain db.internal", b"db.internal\n"),
        ("x" * 63 + ".internal", b"", ("x" * 63 + ".internal\n").encode()),
    ],
)
def test_explicit_domain_overrides_filename_and_normalizes_case(parser, name, contents, expected):
    result = subprocess.run(  # noqa: S603 - fixed read-only harness
        [str(parser), name],
        input=contents,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == expected
    assert result.stderr == b""


@mark.parametrize(
    ("name", "contents"),
    [
        ("db.internal", b"domain\n"),
        ("db.internal", b"domain x extra\n"),
        ("db.internal", b"domain x\ndomain y\n"),
        ("db.internal", b"domain x\ndomain x\n"),
        ("db.internal", b"domain -bad.internal\n"),
        ("db.internal", b"domain bad-.internal\n"),
        ("db.internal", b"domain bad..internal\n"),
        ("db.internal", b"domain .\n"),
        ("db.internal", b"domain bad/internal\n"),
        ("db.internal", b"domain bad\x00.internal\n"),
        ("db.internal", b"domain \xc3\xa9.internal\n"),
        ("db.internal", b"x" * 8193),
        ("x" * 64 + ".internal", b""),
        (".DS_Store", b""),
    ],
)
def test_ambiguous_or_malformed_domain_cannot_mean_no_conflict(parser, name, contents):
    result = subprocess.run(  # noqa: S603 - fixed read-only harness
        [str(parser), name],
        input=contents,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


@mark.parametrize(
    ("first", "second", "overlap"),
    [
        ("db.example.internal", "example.internal", True),
        ("example.internal", "db.example.internal", True),
        ("DB.INTERNAL.", "db.internal", True),
        ("otherdb.internal", "db.internal", False),
        ("db.internal.other", "db.internal", False),
        ("one.internal", "two.internal", False),
    ],
)
def test_conflicts_use_whole_dns_labels_in_both_directions(parser, first, second, overlap):
    result = subprocess.run(  # noqa: S603 - fixed read-only harness
        [str(parser), "--overlap", first, second],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if overlap else 1)
    assert result.stdout == result.stderr == b""


@mark.parametrize(("length", "accepted"), [(253, True), (254, False)])
@mark.parametrize("explicit", [True, False])
def test_total_domain_size_bound_is_independent_of_label_bound(parser, length, accepted, explicit):
    name = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * (length - 192)))
    assert len(name) == length
    result = subprocess.run(  # noqa: S603 - fixed read-only harness
        [str(parser), "invalid/filename" if explicit else name],
        input=(f"domain {name}\n".encode() if explicit else b""),
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if accepted else 1)
    assert result.stdout == (f"{name}\n".encode() if accepted else b"")
    assert result.stderr == b""
