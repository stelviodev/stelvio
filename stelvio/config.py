from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class AwsConfig:
    profile: str | None = "default"
    region: str = "us-east-1"

    def __post_init__(self) -> None:
        if not self.region:
            raise ValueError("AWS region cannot be empty")


@dataclass(frozen=True, kw_only=True)
class StelvioAppConfig:
    aws: AwsConfig
    environments: list[str] = field(default_factory=list)

    def is_valid_environment(self, env: str, username: str) -> bool:
        return env == username or env in self.environments
