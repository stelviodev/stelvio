from types import SimpleNamespace
from unittest.mock import Mock

from stelvio.command_run import (
    _PRELOADED_APP_CONFIGS,
    _invalid_environment_message,
    _load_stlv_app,
    get_environment_confirmation_info,
)
from stelvio.config import StelvioAppConfig


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


def test_environment_confirmation_preloads_config_for_following_load(monkeypatch) -> None:
    app = SimpleNamespace(_name="stelvio-app")
    config = StelvioAppConfig(environments=["prod"])
    load_calls: list[str] = []
    validate_mock = Mock()

    def fake_load(env: str):
        load_calls.append(env)
        return app, config

    monkeypatch.setattr("stelvio.command_run._load_app_config", fake_load)
    monkeypatch.setattr("stelvio.command_run._validate_environment", validate_mock)
    monkeypatch.setattr("stelvio.command_run.ProviderStore.reset", Mock())
    context_set_mock = Mock()
    monkeypatch.setattr("stelvio.command_run._ContextStore.set", context_set_mock)
    _PRELOADED_APP_CONFIGS.clear()

    assert get_environment_confirmation_info("prod") == ("stelvio-app", True)
    _load_stlv_app("prod", dev_mode=False)

    assert load_calls == ["prod"]
    assert validate_mock.call_count == 2
    assert not _PRELOADED_APP_CONFIGS
    context_set_mock.assert_called_once()
