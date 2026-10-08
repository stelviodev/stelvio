from __future__ import annotations

import json
from typing import Any

from pulumi import Output

from stelvio import context


def send_policy(target_arn: Output[str], sender_arn: Output[str]) -> Output[str]:
    """Policy that lets this app's buckets and topics send to an SQS queue or SNS topic.

    Each sender writes this into the target's one policy field, so the text must not depend on
    the sender: per-sender text lets the last write win and cuts the other senders off. The
    scope is every name with the app+env prefix, the narrowest an identical policy can get.
    The sender's ARN is only checked against that scope.

    Create the policy resource with `retain_on_delete=True`: deleting it blanks the queue's
    policy (SQS) or resets the topic's to the AWS default (SNS), which cuts off the senders
    that are left.
    """
    prefix = context().prefix()
    return Output.all(target_arn, sender_arn).apply(lambda arns: _policy(*arns, prefix))


def _policy(target_arn: str, sender_arn: str, prefix: str) -> str:
    _, partition, service, region, account, _ = target_arn.split(":", 5)
    allowed = [f"arn:{partition}:s3:::{prefix}"]
    if service == "sqs":
        allowed.append(f"arn:{partition}:sns:{region}:{account}:{prefix}")

    if not sender_arn.startswith(tuple(allowed)):
        raise ValueError(
            f"{sender_arn} cannot send to {target_arn}: Stelvio's policy on it only lets this "
            f"app's buckets and topics send (names starting with '{prefix}'). Keep that prefix "
            "in the custom name, or pass the target's ARN as a string and write its policy "
            "yourself."
        )

    # StringLike, not ArnLike: ArnLike matches only the region and resource path partially, not
    # a bucket name. Bucket ARNs carry no account, hence SourceAccount.
    # https://docs.aws.amazon.com/AmazonS3/latest/userguide/grant-destinations-permissions-to-s3.html
    condition = {
        "StringEquals": {"aws:SourceAccount": account},
        "StringLike": {"aws:SourceArn": [f"{arn}*" for arn in allowed]},
    }
    statements: list[dict[str, Any]]
    if service == "sqs":
        statements = [
            {
                "Effect": "Allow",
                "Principal": {"Service": ["s3.amazonaws.com", "sns.amazonaws.com"]},
                "Action": "sqs:SendMessage",
                "Resource": target_arn,
                "Condition": condition,
            }
        ]
    else:
        statements = [
            _default_topic_statement(target_arn, account),
            {
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "sns:Publish",
                "Resource": target_arn,
                "Condition": condition,
            },
        ]
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def _default_topic_statement(topic_arn: str, account: str) -> dict[str, Any]:
    """The statement SNS puts on every new topic, which a topic policy replaces.

    CloudWatch alarms, SES and other services publish to a topic through it. Copied from the
    default policy terraform-provider-aws restores when a TopicPolicy is deleted
    (internal/service/sns/topic_policy.go, defaultTopicPolicy).
    """
    return {
        "Sid": "__default_statement_ID",
        "Effect": "Allow",
        "Principal": {"AWS": "*"},
        "Action": [
            "SNS:GetTopicAttributes",
            "SNS:SetTopicAttributes",
            "SNS:AddPermission",
            "SNS:RemovePermission",
            "SNS:DeleteTopic",
            "SNS:Subscribe",
            "SNS:ListSubscriptionsByTopic",
            "SNS:Publish",
            "SNS:Receive",
        ],
        "Resource": topic_arn,
        "Condition": {"StringEquals": {"AWS:SourceOwner": account}},
    }
