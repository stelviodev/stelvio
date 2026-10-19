"""The Python encoder must keep matching independently frozen protocol vectors."""

import base64
import json
from pathlib import Path

from pytest import mark

from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest, ResolverEndpoint


@mark.parametrize("operation", list(HelperOperation))
def test_python_encoder_matches_independent_wire_fixture(operation):
    path = (
        Path(__file__).parents[2]
        / "pkg/traforo-client-go/internal/helper/testdata/independent_vectors.json"
    )
    vectors = json.loads(path.read_text())
    expected = next(
        base64.b64decode(vector["data"])
        for vector in vectors
        if vector["kind"] == "request"
        and vector["returncode"] == 0
        and base64.b64decode(vector["data"])[8] == operation.value
    )
    authenticated = operation in {
        HelperOperation.CONFIGURE,
        HelperOperation.REMOVE,
        HelperOperation.RELEASE,
    }
    scoped = operation in {HelperOperation.CONFIGURE, HelperOperation.REMOVE}
    anonymous = operation in {HelperOperation.INSPECT, HelperOperation.RECONCILE}
    configure = operation == HelperOperation.CONFIGURE
    actual = HelperRequest(
        operation,
        session=None if anonymous else "38321ac9-f086-41ec-91ce-770956914537",
        capability=b"opaque-lease-key" if authenticated else None,
        generation=0x0102030405060708 if scoped else 0,
        unit="00000001" if scoped else None,
        vpc_id="vpc-0123456789abcdef0" if configure else None,
        cidrs=("10.254.0.0/16", "10.253.0.0/16") if configure else (),
        resolvers=(ResolverEndpoint("db.private.test", 10890),) if configure else (),
        keep_dns=operation == HelperOperation.REMOVE,
    ).encode()
    assert actual == expected
