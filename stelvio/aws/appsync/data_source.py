import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final
from urllib.parse import urlparse

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

if TYPE_CHECKING:
    from stelvio.aws.appsync.appsync import AppSync
    from stelvio.aws.dynamo_db import DynamoTable


@final
@dataclass(frozen=True)
class AppSyncDataSourceResources:
    data_source: "appsync.DataSource"
    service_role: "iam.Role"
    function: "Function | None" = None


@final
@dataclass(frozen=True, kw_only=True)
class AppSyncRdsSourceConfig:
    cluster_arn: str
    secret_arn: str
    database: str


@dataclass(frozen=True, kw_only=True)
class AppSyncDataSourceTypeConfig:
    ds_type: str
    handler: "FunctionConfig | Function | None" = None
    table: "DynamoTable | None" = None
    url: str | None = None
    rds: AppSyncRdsSourceConfig | None = None
    endpoint: str | None = None


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


@final
class AppSyncDataSource(Component[AppSyncDataSourceResources, AppSyncDataSourceCustomizationDict]):
    """A data source registered with an AppSync API.

    Created by AppSync builder methods (data_source_lambda, data_source_dynamo, etc.).
    Pass to resolver methods (query, mutation, etc.) to wire resolvers to data sources.
    """

    def __init__(
        self,
        name: str,
        api: "AppSync",
        config: AppSyncDataSourceTypeConfig,
        *,
        tags: dict[str, str] | None = None,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> None:
        super().__init__(
            "stelvio:aws:AppSyncDataSource",
            f"{api.name}-ds-{name}",
            tags=tags,
            customize=customize,
        )

        self._data_source_name = name
        self._api = api
        self._config = config

    @property
    def name(self) -> str:
        return getattr(self, "_data_source_name", self._name)

    @property
    def api(self) -> "AppSync":
        return self._api

    @property
    def ds_type(self) -> str:
        return self._config.ds_type

    @property
    def api_name(self) -> str:
        return self._api.name

    def _create_resources(self) -> AppSyncDataSourceResources:
        prefix = context().prefix
        graphql_api = self._api.resources.api

        role = iam.Role(
            safe_name(prefix(), f"{self._api.name}-ds-{self.name}-role", 64),
            **self._customizer(
                "service_role",
                {"assume_role_policy": _appsync_trust_policy()},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        self._attach_static_policies(role)

        function_instance = self._resolve_lambda_function()
        ds_args: dict[str, Any] = {
            "api_id": graphql_api.id,
            "name": self.name,
            "type": self.ds_type,
            "service_role_arn": role.arn,
        }
        if function_instance is not None:
            ds_args["lambda_config"] = {"function_arn": function_instance.resources.function.arn}
        ds_args.update(self._build_ds_type_config())

        data_source = appsync.DataSource(
            safe_name(prefix(), f"{self._api.name}-ds-{self.name}", 128),
            **self._customizer("data_source", ds_args),
            opts=self._resource_opts(),
        )

        self._attach_output_policies(role, function_instance)
        resources = AppSyncDataSourceResources(
            data_source=data_source,
            service_role=role,
            function=function_instance,
        )
        self.register_outputs(
            {
                "name": self.name,
                "arn": data_source.arn,
                "service_role_arn": role.arn,
            }
        )
        return resources

    def _resolve_lambda_function(self) -> Function | None:
        if self.ds_type != DS_TYPE_LAMBDA:
            return None
        if isinstance(self._config.handler, Function):
            return self._config.handler
        if not isinstance(self._config.handler, FunctionConfig):
            raise TypeError(
                f"Lambda data source '{self.name}' requires a Function or FunctionConfig handler"
            )
        return Function(
            f"{self._api.name}-ds-{self.name}-fn",
            self._config.handler,
            tags=self.tags,
        )

    def _build_ds_type_config(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}

        if self.ds_type == DS_TYPE_DYNAMO:
            if self._config.table is None:
                raise RuntimeError(f"Dynamo data source '{self.name}' requires a table")
            extra["dynamodb_config"] = {
                "table_name": self._config.table.resources.table.name,
                "region": context().aws.region,
            }
        elif self.ds_type == DS_TYPE_HTTP:
            if self._config.url is None:
                raise RuntimeError(f"HTTP data source '{self.name}' requires a url")
            extra["http_config"] = {"endpoint": self._config.url}
        elif self.ds_type == DS_TYPE_RDS:
            if self._config.rds is None:
                raise RuntimeError(f"RDS data source '{self.name}' requires rds config")
            extra["relational_database_config"] = {
                "http_endpoint_config": {
                    "db_cluster_identifier": self._config.rds.cluster_arn,
                    "aws_secret_store_arn": self._config.rds.secret_arn,
                    "database_name": self._config.rds.database,
                },
            }
        elif self.ds_type == DS_TYPE_OPENSEARCH:
            if self._config.endpoint is None:
                raise RuntimeError(f"OpenSearch data source '{self.name}' requires endpoint")
            extra["opensearchservice_config"] = {"endpoint": self._config.endpoint}

        return extra

    def _attach_static_policies(self, role: iam.Role) -> None:
        prefix = context().prefix
        policy_statements: list[dict[str, Any]] = []

        if self.ds_type == DS_TYPE_RDS:
            if self._config.rds is None:
                raise RuntimeError(f"RDS data source '{self.name}' requires rds config")
            policy_statements = [
                {
                    "Effect": "Allow",
                    "Action": [
                        "rds-data:ExecuteStatement",
                        "rds-data:BatchExecuteStatement",
                        "rds-data:BeginTransaction",
                        "rds-data:CommitTransaction",
                        "rds-data:RollbackTransaction",
                    ],
                    "Resource": self._config.rds.cluster_arn,
                },
                {
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": self._config.rds.secret_arn,
                },
            ]
        elif self.ds_type == DS_TYPE_OPENSEARCH:
            if self._config.endpoint is None:
                raise RuntimeError(f"OpenSearch data source '{self.name}' requires endpoint")
            policy_statements = [
                {
                    "Effect": "Allow",
                    "Action": ["es:ESHttp*"],
                    "Resource": _opensearch_arn_from_endpoint(self._config.endpoint),
                },
            ]

        if not policy_statements:
            return

        iam.RolePolicy(
            safe_name(prefix(), f"{self._api.name}-ds-{self.name}-policy", 128),
            role=role.name,
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": policy_statements,
                }
            ),
            opts=self._resource_opts(),
        )

    def _attach_output_policies(
        self,
        role: iam.Role,
        function_instance: Function | None,
    ) -> None:
        prefix = context().prefix

        if self.ds_type == DS_TYPE_LAMBDA and function_instance is not None:
            fn_arn = function_instance.resources.function.arn
            iam.RolePolicy(
                safe_name(prefix(), f"{self._api.name}-ds-{self.name}-lambda-policy", 128),
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
                opts=self._resource_opts(),
            )

        if self.ds_type == DS_TYPE_DYNAMO:
            if self._config.table is None:
                raise RuntimeError(f"Dynamo data source '{self.name}' requires a table")
            iam.RolePolicy(
                safe_name(prefix(), f"{self._api.name}-ds-{self.name}-dynamo-policy", 128),
                role=role.name,
                policy=self._config.table.arn.apply(
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
                opts=self._resource_opts(),
            )
