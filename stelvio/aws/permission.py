from dataclasses import dataclass

from pulumi import Input
from pulumi_aws.iam import GetPolicyDocumentStatementArgsDict


@dataclass(frozen=True)
class AwsPermission:
    actions: list[str] | Input[str]
    resources: list[Input[str]] | Input[str]

    def to_provider_format(self) -> GetPolicyDocumentStatementArgsDict:
        return GetPolicyDocumentStatementArgsDict(actions=self.actions, resources=self.resources)
