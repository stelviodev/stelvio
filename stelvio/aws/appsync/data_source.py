import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

from pulumi_aws import appsync, iam

from stelvio import context
from stelvio.aws.appsync.config import AppSyncDataSourceCustomizationDict
from stelvio.aws.appsync.constants import (
    DS_TYPE_DYNAMO,
    DS_TYPE_HTTP,
    DS_TYPE_LAMBDA,
    DS_TYPE_OPENSEARCH,
    DS_TYPE_RDS,
)
from stelvio.aws.function import Function, FunctionConfig
from stelvio.component import Component, safe_name
from stelvio.pulumi import normalize_pulumi_args_to_dict as _normalize

if TYPE_CHECKING:
    from stelvio.aws.appsync.appsync import AppSync
    from stelvio.aws.dynamo_db import DynamoTable


@final
@dataclass(frozen=True)
class AppSyncDataSourceResources:
    data_source: appsync.DataSource
    service_role: iam.Role
    function: Function | None = None


@final
class AppSyncDataSource(
    Component[AppSyncDataSourceResources, AppSyncDataSourceCustomizationDict]
):
    """A data source registered with an AppSync API.

    Created by AppSync builder methods (data_source_lambda, data_source_dynamo, etc.).
    Pass to resolver methods (query, mutation, etc.) to wire resolvers to data sources.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        api: "AppSync",
        ds_type: str,
        *,
        handler: FunctionConfig | Function | None = None,
        table: "DynamoTable | None" = None,
        url: str | None = None,
        cluster_arn: str | None = None,
        secret_arn: str | None = None,
        database: str | None = None,
        endpoint: str | None = None,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> None:
        self._api = api
        self._ds_name = name
        self._ds_type = ds_type
        self._handler = handler
        self._table = table
        self._url = url
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._database = database
        self._endpoint = endpoint
        super().__init__(f"{api.name}-ds-{name}", customize=customize)

    @property
    def ds_name(self) -> str:
        """The data source name within the AppSync API."""
        return self._ds_name

    @property
    def ds_type(self) -> str:
        return self._ds_type

    def _create_resources(self) -> AppSyncDataSourceResources:
        prefix = context().prefix
        api_id = self._api.resources.api.id

        # Create IAM service role
        role = self._create_role(prefix)

        # Build data source args
        ds_args: dict[str, Any] = {
            "api_id": api_id,
            "name": self._ds_name,
            "type": self._ds_type,
            "service_role_arn": role.arn,
        }

        # Resolve Lambda function if needed
        function_instance = self._resolve_lambda_function()
        if function_instance is not None:
            ds_args["lambda_config"] = {"function_arn": function_instance.resources.function.arn}

        # Add type-specific config
        ds_args.update(self._build_type_config())

        pulumi_ds = appsync.DataSource(
            safe_name(prefix(), f"{self._api.name}-ds-{self._ds_name}", 128),
            **{
                **self._api._customizer("data_source", ds_args),  # noqa: SLF001
                **_normalize(self._customize.get("data_source")),
            },
        )

        resources = AppSyncDataSourceResources(
            data_source=pulumi_ds,
            service_role=role,
            function=function_instance,
        )

        # Create Output-based IAM policies (Lambda ARN, DynamoDB ARN)
        self._create_output_policies(resources, role, prefix)

        return resources

    def _resolve_lambda_function(self) -> Function | None:
        if self._ds_type != DS_TYPE_LAMBDA:
            return None
        if isinstance(self._handler, Function):
            return self._handler
        if isinstance(self._handler, FunctionConfig):
            return Function(f"{self._api.name}-ds-{self._ds_name}-fn", self._handler)
        return None

    def _build_type_config(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}

        if self._ds_type == DS_TYPE_DYNAMO and self._table is not None:
            extra["dynamodb_config"] = {
                "table_name": self._table.resources.table.name,
                "region": context().aws.region,
            }
        elif self._ds_type == DS_TYPE_HTTP and self._url is not None:
            extra["http_config"] = {"endpoint": self._url}
        elif self._ds_type == DS_TYPE_RDS:
            extra["relational_database_config"] = {
                "http_endpoint_config": {
                    "db_cluster_identifier": self._cluster_arn,
                    "aws_secret_store_arn": self._secret_arn,
                    "database_name": self._database,
                },
            }
        elif self._ds_type == DS_TYPE_OPENSEARCH and self._endpoint is not None:
            extra["opensearchservice_config"] = {"endpoint": self._endpoint}

        return extra

    def _create_role(self, prefix: Callable[..., str]) -> iam.Role:
        role_args: dict[str, Any] = {
            "assume_role_policy": _appsync_trust_policy(),
        }
        role = iam.Role(
            safe_name(prefix(), f"{self._api.name}-ds-{self._ds_name}-role", 64),
            **{
                **self._api._customizer("service_role", role_args),  # noqa: SLF001
                **_normalize(self._customize.get("service_role")),
            },
        )

        # Attach static inline policy for RDS and OpenSearch
        policy_statements = self._static_policy_statements()
        if policy_statements:
            iam.RolePolicy(
                safe_name(prefix(), f"{self._api.name}-ds-{self._ds_name}-policy", 128),
                role=role.name,
                policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": policy_statements,
                    }
                ),
            )

        return role

    def _static_policy_statements(self) -> list[dict[str, Any]]:
        if self._ds_type == DS_TYPE_RDS:
            return [
                {
                    "Effect": "Allow",
                    "Action": [
                        "rds-data:ExecuteStatement",
                        "rds-data:BatchExecuteStatement",
                        "rds-data:BeginTransaction",
                        "rds-data:CommitTransaction",
                        "rds-data:RollbackTransaction",
                    ],
                    "Resource": self._cluster_arn,
                },
                {
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": self._secret_arn,
                },
            ]

        if self._ds_type == DS_TYPE_OPENSEARCH and self._endpoint is not None:
            return [
                {
                    "Effect": "Allow",
                    "Action": ["es:ESHttp*"],
                    "Resource": _opensearch_arn_from_endpoint(self._endpoint),
                },
            ]

        return []

    def _create_output_policies(
        self,
        resources: AppSyncDataSourceResources,
        role: iam.Role,
        prefix: Callable[..., str],
    ) -> None:
        if self._ds_type == DS_TYPE_LAMBDA and resources.function is not None:
            fn_arn = resources.function.resources.function.arn
            iam.RolePolicy(
                safe_name(prefix(), f"{self._api.name}-ds-{self._ds_name}-lambda-policy", 128),
                role=role.name,
                policy=fn_arn.apply(
                    lambda arn: json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": ["lambda:InvokeFunction"],
                                    "Resource": arn,
                                }
                            ],
                        }
                    )
                ),
            )

        elif self._ds_type == DS_TYPE_DYNAMO and self._table is not None:
            iam.RolePolicy(
                safe_name(prefix(), f"{self._api.name}-ds-{self._ds_name}-dynamo-policy", 128),
                role=role.name,
                policy=self._table.arn.apply(
                    lambda arn: json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": [
                                        "dynamodb:GetItem",
                                        "dynamodb:PutItem",
                                        "dynamodb:UpdateItem",
                                        "dynamodb:DeleteItem",
                                        "dynamodb:Query",
                                        "dynamodb:Scan",
                                    ],
                                    "Resource": [arn, f"{arn}/index/*"],
                                }
                            ],
                        }
                    )
                ),
            )


def _appsync_trust_policy() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "appsync.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def _opensearch_arn_from_endpoint(endpoint: str) -> str:
    """Derive OpenSearch domain ARN pattern from endpoint URL for IAM policy Resource."""
    import re
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.netloc

    if parsed.scheme != "https" or not host or parsed.path not in ("", "/"):
        raise ValueError(
            f"Cannot derive domain ARN from OpenSearch endpoint '{endpoint}'. "
            "Expected format: https://search-DOMAIN-ID.REGION.es.amazonaws.com"
        )

    suffix = ".es.amazonaws.com"
    if not host.endswith(suffix):
        raise ValueError(
            f"Cannot derive domain ARN from OpenSearch endpoint '{endpoint}'. "
            "Expected format: https://search-DOMAIN-ID.REGION.es.amazonaws.com"
        )

    host_without_suffix = host[: -len(suffix)]
    domain_part, separator, region = host_without_suffix.rpartition(".")
    if not separator or not domain_part or not region or not re.fullmatch(r"[a-z0-9-]+", region):
        raise ValueError(
            f"Cannot derive domain ARN from OpenSearch endpoint '{endpoint}'. "
            "Expected format: https://search-DOMAIN-ID.REGION.es.amazonaws.com"
        )

    if domain_part.startswith("search-"):
        domain_with_id = domain_part.removeprefix("search-")
    elif domain_part.startswith("vpc-"):
        domain_with_id = domain_part.removeprefix("vpc-")
    else:
        raise ValueError(
            f"Cannot derive domain ARN from OpenSearch endpoint '{endpoint}'. "
            "Expected format: https://search-DOMAIN-ID.REGION.es.amazonaws.com"
        )

    domain_name, id_separator, domain_id = domain_with_id.rpartition("-")
    if (
        not id_separator
        or not domain_name
        or not domain_id
        or not re.fullmatch(r"[a-z0-9-]+", domain_name)
        or not re.fullmatch(r"[a-z0-9]+", domain_id)
    ):
        raise ValueError(
            f"Cannot derive domain ARN from OpenSearch endpoint '{endpoint}'. "
            "Expected format: https://search-DOMAIN-ID.REGION.es.amazonaws.com"
        )

    return f"arn:aws:es:{region}:*:domain/{domain_name}/*"
