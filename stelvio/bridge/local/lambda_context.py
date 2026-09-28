"""Local Lambda context mock for `stlv dev` bridge invocations.

Replaces awslambdaric.LambdaContext so Stelvio does not need that package
(no wheel on macOS/Windows; install would compile the C RIC client).
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any


class CognitoIdentity:
    __slots__ = ["cognito_identity_id", "cognito_identity_pool_id"]

    def __init__(
        self,
        cognito_identity_id: str | None = None,
        cognito_identity_pool_id: str | None = None,
    ) -> None:
        self.cognito_identity_id = cognito_identity_id
        self.cognito_identity_pool_id = cognito_identity_pool_id


class Client:
    __slots__ = [
        "app_package_name",
        "app_title",
        "app_version_code",
        "app_version_name",
        "installation_id",
    ]

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        for field in self.__slots__:
            setattr(self, field, data.get(field))


class ClientContext:
    __slots__ = ["client", "custom", "env"]

    def __init__(self, data: dict[str, Any]) -> None:
        self.custom = data.get("custom")
        self.env = data.get("env")
        client = data.get("client")
        self.client = Client(client) if client is not None else None


def _cognito_field(cognito_identity: dict[str, Any], snake: str, camel: str) -> str | None:
    if snake in cognito_identity:
        return cognito_identity[snake]
    return cognito_identity.get(camel)


class LambdaContext:
    """Minimal Lambda context matching the surface handlers use in `stlv dev`."""

    def __init__(  # noqa: PLR0913  # matches awslambdaric / stub kwargs
        self,
        invoke_id: str,
        client_context: dict[str, Any] | None,
        cognito_identity: dict[str, Any] | None,
        epoch_deadline_time_in_ms: int | None,
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

        self.client_context = ClientContext(client_context) if client_context is not None else None

        if cognito_identity is None:
            self.identity = CognitoIdentity()
        else:
            self.identity = CognitoIdentity(
                cognito_identity_id=_cognito_field(
                    cognito_identity, "cognito_identity_id", "cognitoIdentityId"
                ),
                cognito_identity_pool_id=_cognito_field(
                    cognito_identity, "cognito_identity_pool_id", "cognitoIdentityPoolId"
                ),
            )

        self._epoch_deadline_time_in_ms = epoch_deadline_time_in_ms

    def get_remaining_time_in_millis(self) -> int:
        if self._epoch_deadline_time_in_ms is None:
            return 0
        epoch_now_in_ms = int(time.time() * 1000)
        delta_ms = self._epoch_deadline_time_in_ms - epoch_now_in_ms
        return max(0, delta_ms)

    def log(self, msg: object) -> None:
        sys.stdout.write(str(msg))
