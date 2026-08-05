import re
from typing import Literal

VALID_LOG_RETENTION_DAYS = {
    1,
    3,
    5,
    7,
    14,
    30,
    60,
    90,
    120,
    150,
    180,
    365,
    400,
    545,
    731,
    1096,
    1827,
    2192,
    2557,
    2922,
    3288,
    3653,
}

DOMAIN_LABEL_MAX_LENGTH = 63
DOMAIN_NAME_MAX_LENGTH = 253
DOMAIN_MIN_LABELS = 2
STAGE_NAME_MAX_LENGTH = 128


def validate_stage_name(stage_name: str) -> None:
    if len(stage_name) > STAGE_NAME_MAX_LENGTH:
        raise ValueError(f"Stage name must be at most {STAGE_NAME_MAX_LENGTH} characters")
    if stage_name.startswith("$"):
        if stage_name != "$default":
            raise ValueError(
                f"Stage name starting with '$' must be exactly '$default', got {stage_name!r}"
            )
        return
    if not re.match(r"^[a-zA-Z0-9_-]+$", stage_name):
        raise ValueError(
            f"Stage name must contain only alphanumerics, hyphens, and underscores, "
            f"got {stage_name!r}"
        )


def validate_log_retention_days(value: int | Literal["forever"] | None) -> None:
    if value is None or value == "forever":
        return
    if value not in VALID_LOG_RETENTION_DAYS:
        raise ValueError(
            f"Invalid access_log_retention_days={value!r}. "
            f"Must be 'forever' or one of: {sorted(VALID_LOG_RETENTION_DAYS)}"
        )


def validate_api_mapping_key(key: str, *, field_name: str = "api_mapping_key") -> None:
    if not key:
        raise ValueError(f"{field_name} cannot be empty string (use None for root mapping)")
    if key.startswith("/") or key.endswith("/"):
        raise ValueError(f"{field_name} must not start or end with '/', got {key!r}")
    if "//" in key:
        raise ValueError(f"{field_name} must not contain empty path segments (//), got {key!r}")


def validate_domain_name(value: str, *, field_name: str = "domain_name") -> None:
    if not isinstance(value, str):
        raise TypeError(f"{_display_name(field_name)} must be a string")
    domain = value.strip()
    if not domain:
        raise ValueError(f"{_display_name(field_name)} cannot be empty")
    if domain != value:
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    if len(domain) > DOMAIN_NAME_MAX_LENGTH:
        raise ValueError(f"{field_name} must be at most {DOMAIN_NAME_MAX_LENGTH} characters")
    labels = domain.rstrip(".").split(".")
    if len(labels) < DOMAIN_MIN_LABELS:
        raise ValueError(f"{_display_name(field_name)} must include at least one dot")
    for label in labels:
        _validate_domain_label(label, field_name)


def _validate_domain_label(label: str, field_name: str) -> None:
    if not label:
        raise ValueError(f"{_display_name(field_name)} must not contain empty labels")
    if len(label) > DOMAIN_LABEL_MAX_LENGTH:
        raise ValueError(
            f"{field_name} labels must be at most {DOMAIN_LABEL_MAX_LENGTH} characters"
        )
    if label.startswith("-") or label.endswith("-"):
        raise ValueError(f"{_display_name(field_name)} labels must not start or end with '-'")
    if not all(char.isalnum() or char == "-" for char in label):
        raise ValueError(
            f"{_display_name(field_name)} labels may contain only letters, numbers, and hyphens"
        )


def _validate_path_param(path: str, param: str) -> None:
    if param.endswith("+"):
        if param != "proxy+":
            raise ValueError("Only {proxy+} is supported for greedy paths")
        param_pos = path.index(f"{{{param}}}")
        if param_pos != len(path) - len(f"{{{param}}}"):
            raise ValueError("Greedy parameter must be at the end of the path")
        return
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", param):
        raise ValueError(f"Invalid parameter name: {param}")


def _display_name(field_name: str) -> str:
    return "Domain name" if field_name == "domain_name" else field_name
