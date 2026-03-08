import pytest

from stelvio.config import StelvioAppConfig


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
