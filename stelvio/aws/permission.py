from collections.abc import Sequence
from dataclasses import dataclass
from typing import final

from pulumi import Input
from pulumi_aws.iam import GetPolicyDocumentStatementArgs


@final
@dataclass(frozen=True)
class AwsPermission:
    actions: Sequence[str]
    resources: Sequence[Input[str]]

    def to_provider_format(self) -> GetPolicyDocumentStatementArgs:
        return GetPolicyDocumentStatementArgs(actions=self.actions, resources=self.resources)
