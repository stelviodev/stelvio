import pytest

from stelvio.config import AwsConfig, StelvioAppConfig


@pytest.fixture
def isolated_aws_config(monkeypatch, tmp_path):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))


def test_aws_config_defaults_region_to_us_east_1_when_unconfigured(isolated_aws_config):
    config = AwsConfig()

    assert config.region == "us-east-1"


def test_aws_config_uses_region_from_environment(monkeypatch, isolated_aws_config):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    config = AwsConfig()

    assert config.region == "eu-west-1"


def test_aws_config_uses_region_from_profile(monkeypatch, tmp_path, isolated_aws_config):
    config_file = tmp_path / "config"
    config_file.write_text("[profile deploy]\nregion = ap-southeast-1\n")

    config = AwsConfig(profile="deploy")

    assert config.region == "ap-southeast-1"


def test_aws_config_preserves_explicit_region(monkeypatch, isolated_aws_config):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    config = AwsConfig(region="us-west-2")

    assert config.region == "us-west-2"


def test_stelvio_app_config_normalizes_none_tags_to_empty_dict():
    config = StelvioAppConfig(tags=None)
    assert config.tags == {}


def test_stelvio_app_config_rejects_non_dict_tags():
    with pytest.raises(TypeError, match="expected dict\\[str, str\\] or None"):
        StelvioAppConfig(tags=["bad"])  # type: ignore[arg-type]


def test_stelvio_app_config_rejects_non_string_tag_values():
    with pytest.raises(TypeError, match="string keys and values"):
        StelvioAppConfig(tags={"Team": 123})  # type: ignore[dict-item]


def test_stelvio_app_config_rejects_non_string_tag_keys():
    with pytest.raises(TypeError, match="string keys and values"):
        StelvioAppConfig(tags={123: "platform"})  # type: ignore[dict-item]
