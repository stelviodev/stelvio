"""Disposable proof logs are deleted only after ownership/state gates pass."""

import io
import json
from unittest.mock import Mock, call

from pytest import mark, raises

from tests.integration.tunnel_cleanup import _remove_lambda_logs, purge_test_metadata


def test_log_cleanup_checks_all_names_before_first_delete():
    sdk = Mock()
    logs = sdk.client.return_value
    logs.get_paginator.return_value.paginate.return_value = [
        {"logGroups": [{"logGroupName": "/aws/lambda/stlv-abc123-test-api-1234567"}]},
        {"logGroups": [{"logGroupName": "/aws/lambda/foreign-app-api"}]},
    ]
    with raises(AssertionError):
        _remove_lambda_logs(sdk, "stlv-abc123", "test")
    logs.delete_log_group.assert_not_called()


def test_log_cleanup_deletes_exact_inventory_and_confirms_absence():
    sdk = Mock()
    logs = sdk.client.return_value
    name = "/aws/lambda/stlv-abc123-test-api-1234567"
    logs.get_paginator.return_value.paginate.side_effect = [
        [{"logGroups": [{"logGroupName": name}]}],
        [{"logGroups": []}],
    ]
    assert _remove_lambda_logs(sdk, "stlv-abc123", "test") == 1
    logs.delete_log_group.assert_called_once_with(logGroupName=name)
    assert logs.get_paginator.return_value.paginate.call_args_list == [
        call(logGroupNamePrefix="/aws/lambda/stlv-abc123-test-"),
        call(logGroupNamePrefix="/aws/lambda/stlv-abc123-test-"),
    ]


def test_log_cleanup_retained_group_is_not_certified_empty():
    sdk = Mock()
    logs = sdk.client.return_value
    logs.get_paginator.return_value.paginate.return_value = [
        {"logGroups": [{"logGroupName": "/aws/lambda/stlv-abc123-test-api-1234567"}]},
    ]
    with raises(AssertionError):
        _remove_lambda_logs(sdk, "stlv-abc123", "test")


@mark.parametrize("pending", ["resources", "pending_operations"])
def test_metadata_cleanup_refuses_live_state_before_log_deletion(pending):
    sdk = Mock()
    sts, s3, logs = Mock(), Mock(), Mock()
    sdk.client.side_effect = lambda name, **_kwargs: {"sts": sts, "s3": s3, "logs": logs}[name]
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s3.get_object.return_value = {
        "Body": io.BytesIO(
            json.dumps(
                {
                    "checkpoint": {"latest": {pending: [{}]}},
                }
            ).encode()
        )
    }
    with raises(AssertionError):
        purge_test_metadata(
            sdk, app="stlv-abc123", env="test", bucket="owned-state", account="123456789012"
        )
    logs.delete_log_group.assert_not_called()
    s3.delete_objects.assert_not_called()


def test_metadata_cleanup_wrong_account_never_deletes_resources():
    sdk = Mock()
    sdk.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
    with raises(AssertionError):
        purge_test_metadata(
            sdk, app="stlv-abc123", env="test", bucket="owned-state", account="123456789012"
        )
    assert sdk.client.call_count == 1
    sdk.client.return_value.delete_log_group.assert_not_called()
    sdk.client.return_value.delete_objects.assert_not_called()


def test_empty_checkpoint_with_live_aws_vpc_refuses_all_deletions():
    sdk = Mock()
    sts, s3, ec2, logs, ssm = Mock(), Mock(), Mock(), Mock(), Mock()
    clients = {"sts": sts, "s3": s3, "ec2": ec2, "logs": logs, "ssm": ssm}
    sdk.client.side_effect = lambda name, **_kwargs: clients[name]
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s3.get_object.return_value = {
        "Body": io.BytesIO(
            json.dumps(
                {
                    "checkpoint": {"latest": {}},
                }
            ).encode()
        )
    }
    ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-still-owned"}]}
    with raises(AssertionError):
        purge_test_metadata(
            sdk, app="stlv-abc123", env="test", bucket="owned-state", account="123456789012"
        )
    ec2.describe_vpcs.assert_called_once_with(
        Filters=[
            {"Name": "tag:stelvio:app", "Values": ["stlv-abc123"]},
        ]
    )
    logs.delete_log_group.assert_not_called()
    s3.delete_objects.assert_not_called()
    ssm.delete_parameter.assert_not_called()
