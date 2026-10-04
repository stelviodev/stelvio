"""Local Lambda context mock for `stlv dev` bridge invocations.

Replaces awslambdaric.LambdaContext so Stelvio does not need that package
(no wheel on macOS/Windows; install would compile the C RIC client).
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Any, final


@final
@dataclass(frozen=True)
class CognitoIdentity:
    cognito_identity_id: str | None = None
    cognito_identity_pool_id: str | None = None


@final
@dataclass(frozen=True)
class Client:
    installation_id: str | None = None
    app_title: str | None = None
    app_version_name: str | None = None
    app_version_code: str | None = None
    app_package_name: str | None = None


@final
@dataclass(frozen=True)
class ClientContext:
    client: Client | None = None
    custom: Any = None
    env: Any = None


def _client_from_dict(data: dict[str, Any]) -> Client:
    return Client(
        installation_id=data.get("installation_id"),
        app_title=data.get("app_title"),
        app_version_name=data.get("app_version_name"),
        app_version_code=data.get("app_version_code"),
        app_package_name=data.get("app_package_name"),
    )


def _client_context_from_dict(data: dict[str, Any]) -> ClientContext:
    client_data = data.get("client")
    client = _client_from_dict(client_data) if client_data is not None else None
    return ClientContext(client=client, custom=data.get("custom"), env=data.get("env"))


@final
class LambdaContext:
    """Minimal Lambda context matching the surface handlers use in `stlv dev`."""

    def __init__(  # noqa: PLR0913  # matches awslambdaric / stub kwargs
        self,
        invoke_id: str,
        client_context: dict[str, Any] | None,
        cognito_identity: dict[str, Any] | None,
        epoch_deadline_time_in_ms: int,
        invoked_function_arn: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.aws_request_id = invoke_id
        self.log_group_name = os.environ.get("AWS_LAMBDA_LOG_GROUP_NAME")
        self.log_stream_name = os.environ.get("AWS_LAMBDA_LOG_STREAM_NAME")
        self.function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        self.memory_limit_in_mb = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
        self.function_version = os.environ.get("AWS_LAMBDA_FUNCTION_VERSION")
        self.invoked_function_arn = invoked_function_arn
        self.tenant_id = tenant_id

        self.client_context = (
            _client_context_from_dict(client_context) if client_context is not None else None
        )

        if cognito_identity is None:
            self.identity = CognitoIdentity()
        else:
            self.identity = CognitoIdentity(
                cognito_identity_id=cognito_identity.get("cognito_identity_id"),
                cognito_identity_pool_id=cognito_identity.get("cognito_identity_pool_id"),
            )

        self._epoch_deadline_time_in_ms = epoch_deadline_time_in_ms

    def get_remaining_time_in_millis(self) -> int:
        epoch_now_in_ms = int(time.time() * 1000)
        delta_ms = self._epoch_deadline_time_in_ms - epoch_now_in_ms
        return max(0, delta_ms)

    def log(self, msg: object) -> None:
        sys.stdout.write(str(msg))
