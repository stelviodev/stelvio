import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.app import StelvioApp
from stelvio.command_run import (
    _PRELOADED_APP_CONFIGS,
    _create_stack,
    _invalid_environment_message,
    _load_stlv_app,
    get_environment_confirmation_info,
)
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.exceptions import StelvioValidationError


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


def _confirm_env(
    monkeypatch, env: str, *, user_env: str, app_name: str = "app"
) -> tuple[str, bool]:
    config = StelvioAppConfig(environments=[env])
    monkeypatch.setattr(
        "stelvio.command_run._load_app_config",
        lambda _: (SimpleNamespace(_name=app_name), config),
    )
    monkeypatch.setattr("stelvio.command_run.get_user_env", lambda: user_env)
    return get_environment_confirmation_info(env)


@pytest.mark.parametrize(
    "name", ["my_app", "my.app", "my app", "1app", "-app", "app-", "a--b", "", "app\n"]
)
def test_app_name_outside_letters_digits_and_hyphen_is_rejected(monkeypatch, name) -> None:
    with pytest.raises(StelvioValidationError) as exc:
        _confirm_env(monkeypatch, "dev", user_env="michal", app_name=name)

    assert str(exc.value) == (
        f"Invalid app name '{name}': use only letters, digits and single '-' between them, "
        "starting with a letter. Destroy a deployed stack under this name before you rename it."
    )


@pytest.mark.parametrize("name", ["a", "My-App-2", "a1-b2-c3"])
def test_app_name_allows_letters_digits_and_hyphens(monkeypatch, name) -> None:
    assert _confirm_env(monkeypatch, "dev", user_env="michal", app_name=name) == (name, True)


def _load(monkeypatch, env: str, *, app_name: str, check_names: bool) -> Mock:
    config = StelvioAppConfig(environments=[env])
    monkeypatch.setattr(
        "stelvio.command_run._load_app_config",
        lambda _: (SimpleNamespace(_name=app_name), config),
    )
    monkeypatch.setattr("stelvio.command_run.get_user_env", lambda: "michal")
    monkeypatch.setattr("stelvio.command_run.ProviderStore.reset", Mock())
    context_set = Mock()
    monkeypatch.setattr("stelvio.command_run._ContextStore.set", context_set)
    _load_stlv_app(env, dev_mode=False, check_names=check_names)
    return context_set


def test_load_without_the_name_check_takes_names_deploy_rejects(monkeypatch) -> None:
    """Destroy, refresh and the state commands: a stack deployed before the name rule must
    still load, or its resources can only be removed by hand."""
    context_set = _load(monkeypatch, "john.doe", app_name="my_app", check_names=False)

    context_set.assert_called_once()


def test_load_with_the_name_check_rejects_the_app_name(monkeypatch) -> None:
    with pytest.raises(StelvioValidationError, match=r"^Invalid app name 'my_app'"):
        _load(monkeypatch, "john.doe", app_name="my_app", check_names=True)


@pytest.mark.parametrize("env", ["my_env", "john.doe", "a b", "-dev", "dev-", "a--b", "dev\n"])
def test_env_name_outside_letters_digits_and_hyphen_is_rejected(monkeypatch, env) -> None:
    with pytest.raises(StelvioValidationError) as exc:
        _confirm_env(monkeypatch, env, user_env="michal")

    assert str(exc.value) == (
        f"Invalid environment name '{env}': use only letters, digits and single '-' between them. "
        "Destroy a deployed stack under this name before you rename it."
    )


def test_invalid_personal_env_name_points_at_userenv(monkeypatch) -> None:
    with pytest.raises(StelvioValidationError) as exc:
        _confirm_env(monkeypatch, "john.doe", user_env="john.doe")

    assert str(exc.value) == (
        "Invalid environment name 'john.doe': use only letters, digits and single '-' between "
        "them. Your personal environment is set in .stelvio/userenv. Destroy a deployed stack "
        "under it before you change it."
    )


def test_env_name_allows_uppercase_digits_and_hyphens(monkeypatch) -> None:
    assert _confirm_env(monkeypatch, "Staging-2", user_env="michal") == ("app", True)


@pytest.fixture
def stelvio_app():
    app = StelvioApp("test-app")
    yield app
    StelvioApp._StelvioApp__instance = None  # type: ignore[attr-defined]


def test_default_config_when_no_app_config_registered(stelvio_app) -> None:
    config = stelvio_app._execute_user_config_func("dev")

    assert isinstance(config, StelvioAppConfig)
    assert config.aws == AwsConfig()
    assert config.environments == []
    assert config.tags == {}
    assert config.dns is None
    assert config.customize == {}
    assert config.home == "aws"


def test_user_config_returned_when_app_config_registered(stelvio_app) -> None:
    user_config = StelvioAppConfig(
        aws=AwsConfig(profile="my-profile"),
        environments=["staging", "prod"],
    )

    @stelvio_app.config
    def configuration(env: str) -> StelvioAppConfig:
        return user_config

    config = stelvio_app._execute_user_config_func("prod")

    assert config is user_config
    assert config.aws.profile == "my-profile"
    assert config.environments == ["staging", "prod"]


def test_env_passed_to_config_function(stelvio_app) -> None:
    received_env = None

    @stelvio_app.config
    def configuration(env: str) -> StelvioAppConfig:
        nonlocal received_env
        received_env = env
        return StelvioAppConfig()

    stelvio_app._execute_user_config_func("staging")

    assert received_env == "staging"


def test_app_config_returning_none_raises_value_error(stelvio_app) -> None:
    @stelvio_app.config
    def configuration(env: str) -> StelvioAppConfig:
        return None  # type: ignore[return-value]

    with pytest.raises(ValueError, match="must return an instance of StelvioAppConfig"):
        stelvio_app._execute_user_config_func("dev")


def test_stack_config_gives_default_provider_the_app_tags(
    monkeypatch, stelvio_app, tmp_path
) -> None:
    stelvio_app.run(lambda: None)
    ctx = AppContext(
        name="test-app",
        env="dev",
        aws=AwsConfig(region="us-east-1"),
        home="aws",
        tags={"commit": "1234e56"},
    )
    _ContextStore.clear()
    _ContextStore.set(ctx)
    monkeypatch.setattr("stelvio.command_run.PulumiCommand", Mock())
    create_or_select_stack = Mock()
    monkeypatch.setattr("stelvio.command_run.create_or_select_stack", create_or_select_stack)

    _create_stack(ctx, "passphrase", tmp_path)

    kwargs = create_or_select_stack.call_args.kwargs
    stack_settings = kwargs["opts"].stack_settings
    assert list(stack_settings) == [kwargs["stack_name"]]
    config = stack_settings[kwargs["stack_name"]].config
    assert json.loads(config["aws:defaultTags"]) == {
        "tags": {"stelvio:app": "test-app", "stelvio:env": "dev", "commit": "1234e56"}
    }
