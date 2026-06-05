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
    1827,
    3653,
}


def validate_log_retention_days(value: int | None) -> None:
    if value is None:
        return
    if value not in VALID_LOG_RETENTION_DAYS:
        raise ValueError(
            f"Invalid access_log_retention_days={value!r}. "
            f"Must be None or one of: {sorted(VALID_LOG_RETENTION_DAYS)}"
        )


def validate_api_mapping_key(key: str, *, field_name: str = "api_mapping_key") -> None:
    if not key:
        raise ValueError(f"{field_name} cannot be empty string (use None for root mapping)")
    if key.startswith("/") or key.endswith("/"):
        raise ValueError(f"{field_name} must not start or end with '/', got {key!r}")
    if "//" in key:
        raise ValueError(f"{field_name} must not contain empty path segments (//), got {key!r}")
