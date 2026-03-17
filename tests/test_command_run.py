from stelvio.command_run import _invalid_environment_message


def test_invalid_environment_message_without_shared_environments() -> None:
    message = _invalid_environment_message("prod", "michal", [])

    assert message == (
        "Invalid environment 'prod'. "
        "Use your username 'michal' for a personal environment. "
        "No shared environments are configured. "
        "Set `environments=[...]` in `StelvioAppConfig(...)` to allow named shared environments."
    )


def test_invalid_environment_message_with_shared_environments() -> None:
    message = _invalid_environment_message("prod", "michal", ["staging", "production"])

    assert message == (
        "Invalid environment 'prod'. "
        "Use your username 'michal' for a personal environment. "
        "Configured shared environments: 'staging', 'production'."
    )
