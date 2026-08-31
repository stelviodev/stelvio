"""Shared helpers for HttpApi and WebsocketApi unit tests."""

from __future__ import annotations

import json
from typing import Any

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, TP, R, tid, tn

LAMBDA_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["lambda.amazonaws.com"], "type": "Service"}],
    }
]

API_DOMAIN_GRAPH_COUNTS = {
    R.CERTIFICATE: 1,
    R.CLOUDFLARE_RECORD: 2,
    R.CERTIFICATE_VALIDATION: 1,
    R.HTTP_API_DOMAIN_NAME: 1,
}


def assert_api_domain_graph(
    mocks,
    *,
    domain_component: str,
    domain_name: str,
    domain_extra_inputs: dict[str, Any] | None = None,
    dns_record_extra_inputs: dict[str, Any] | None = None,
) -> None:
    """Assert ACM, DomainName, and DNS resources for an ApiDomain."""
    certificate_arn = (
        f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/"
        f"{tid(TP + f'{domain_component}-cert-certificate')}"
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate",
        R.CERTIFICATE,
        {"domainName": domain_name, "validationMethod": "DNS"},
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate-validation",
        R.CERTIFICATE_VALIDATION,
        {
            "certificateArn": certificate_arn,
            "validationRecordFqdns": [f"_test.{domain_name}"],
        },
    )
    domain_inputs: dict[str, Any] = {
        "domainName": domain_name,
        "domainNameConfiguration": {
            "certificateArn": certificate_arn,
            "endpointType": "REGIONAL",
            "securityPolicy": "TLS_1_2",
        },
    }
    if domain_extra_inputs:
        domain_inputs.update(domain_extra_inputs)
    mocks.assert_res(
        f"{domain_component}-domain",
        R.HTTP_API_DOMAIN_NAME,
        domain_inputs,
        partial=domain_extra_inputs is not None,
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate-validation-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": f"_test.{domain_name}",
            "type": "CNAME",
            "content": f"test-validation.{domain_name}",
            "ttl": 1.0,
            "zoneId": "test-zone-id",
        },
    )
    dns_inputs: dict[str, Any] = {
        "name": domain_name,
        "type": "CNAME",
        "content": (
            f"d-{tid(TP + f'{domain_component}-domain')}"
            f".execute-api.{DEFAULT_REGION}.amazonaws.com"
        ),
        "ttl": 300.0,
        "zoneId": "test-zone-id",
    }
    if dns_record_extra_inputs:
        dns_inputs.update(dns_record_extra_inputs)
    mocks.assert_res(
        f"{domain_component}-dns-record",
        R.CLOUDFLARE_RECORD,
        dns_inputs,
    )


def assert_lambda_role_and_attachment(mocks, function_name: str) -> None:
    role_name = f"{function_name}-r"
    mocks.assert_res(
        role_name,
        R.ROLE,
        {"assumeRolePolicy": json.dumps(LAMBDA_ASSUME_ROLE_POLICY)},
    )
    mocks.assert_res(
        f"{function_name}-basic-execution-r-p-attachment",
        R.ROLE_POLICY_ATTACHMENT,
        {
            "policyArn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "role": tn(TP + role_name),
        },
    )
