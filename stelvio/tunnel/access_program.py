"""The temporary access stack program, independent of application evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, final

import pulumi
import pulumi_aws as aws

from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.tunnel.bastion import create_bastion

_ACCESS_CONTEXT: AppContext | None = None


def prepare_access_context(region: str) -> None:
    """Initialize only a fresh worker's private context, without app evaluation."""
    global _ACCESS_CONTEXT  # noqa: PLW0603 - the process owns its isolated SDK context
    if _ACCESS_CONTEXT is not None:
        return
    private = AppContext(
        name="stelvio-tunnel", env="access", aws=AwsConfig(region=region), home="aws"
    )
    _ContextStore.set(private)
    _ACCESS_CONTEXT = private


if TYPE_CHECKING:
    import boto3

    from stelvio.tunnel.access_state import AccessJournal


@final
@dataclass(frozen=True)
class CapturedCredentials:
    account: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    token: str | None = field(repr=False)

    @classmethod
    def capture(cls, session: boto3.Session) -> CapturedCredentials:
        account = session.client("sts").get_caller_identity()["Account"]
        credentials = session.get_credentials().get_frozen_credentials()
        if not session.region_name:
            raise ValueError("Temporary access requires an explicit resolved region")
        return cls(
            account,
            session.region_name,
            credentials.access_key,
            credentials.secret_key,
            credentials.token,
        )


def access_program(journal: AccessJournal, credentials: CapturedCredentials) -> None:
    """Register only access-owned resources after durable identity/intent checks."""
    journal.require_claim()
    intent = journal.intent
    if credentials.account != intent.account or credentials.region != intent.region:
        raise RuntimeError("Temporary access credential identity differs; mutations refused")
    provider = aws.Provider(
        "tunnel-aws",
        region=intent.region,
        allowed_account_ids=[intent.account],
    )
    opts = pulumi.ResourceOptions(provider=provider)
    # Planned AWS names and atomic tags recover creates missing from a checkpoint.
    names = {
        "bastion_security_group": "-sg",
        "bastion_role": "-role",
        "bastion_profile": "-profile",
        "bastion_identity_document": "-identity",
    }

    def customize(key: str, properties: dict[str, Any], *, inject_tags: bool) -> dict[str, Any]:
        result = dict(properties)
        if key in names:
            result["name"] = intent.name + names[key]
        if key == "bastion":
            result["availability_zone"] = intent.availability_zone
            result["user_data"] = intent.user_data_content
        if key == "bastion_identity_document":
            result["content"] = intent.identity_document_content
        if key == "bastion_role":
            result["assume_role_policy"] = intent.assume_role_policy_content
        if inject_tags:
            result["tags"] = intent.tags
        return result

    access = create_bastion(
        name=intent.name,
        vpc_id=intent.vpc_id,
        subnet_id=intent.subnet_id,
        tags=intent.tags,
        opts=opts,
        customize=customize,
        ami=intent.ami,
        ssm_permissions=intent.ssm_policy_content,
    )
    for index, target in enumerate(intent.targets):
        aws.vpc.SecurityGroupIngressRule(
            f"service-access-{index}",
            security_group_id=target.security_group_id,
            referenced_security_group_id=access.security_group.id,
            ip_protocol="tcp",
            from_port=target.port,
            to_port=target.port,
            tags=intent.tags,
            opts=opts,
        )
    pulumi.export(
        "access",
        {
            "instance_id": access.instance.id,
            "security_group_id": access.security_group.id,
            "az": access.instance.availability_zone,
            "identity_document": access.document.name,
            "owner": intent.session,
        },
    )
