"""Key checks for components whose customize keys can't be read.

No `from __future__ import annotations` here: the 3.14 test needs eagerly declared,
lazily evaluated annotations (PEP 649).
"""

import sys
from dataclasses import dataclass
from typing import Any, TypedDict

from pytest import mark, raises

from stelvio.component import Component
from stelvio.config import StelvioAppConfig
from stelvio.customize import ChildCustomization, Customization
from stelvio.provider import ProviderStore


@dataclass(frozen=True)
class _Res:
    value: int


class _DictAnyComponent(Component[_Res, dict[str, Any]]):
    def __init__(self, name: str, *, customize: dict[str, Any] | None = None):
        super().__init__(ProviderStore.aws(), "stelvio:test:DictAny", name, customize=customize)

    def _create_resources(self) -> _Res:
        return _Res(1)


class _AnyComponent(Component[_Res, Any]):
    def __init__(self, name: str, *, customize: Any = None):
        super().__init__(ProviderStore.aws(), "stelvio:test:Any", name, customize=customize)

    def _create_resources(self) -> _Res:
        return _Res(1)


class _PlainDictComponent(Component[_Res, dict]):
    def __init__(self, name: str, *, customize: dict | None = None):
        super().__init__(ProviderStore.aws(), "stelvio:test:Dict", name, customize=customize)

    def _create_resources(self) -> _Res:
        return _Res(1)


@mark.parametrize("component_type", [_PlainDictComponent, _DictAnyComponent, _AnyComponent])
def test_app_wide_customize_for_untyped_component_passes(component_type):
    customize = {component_type: {"bucket": {"force_destroy": True}}}

    assert StelvioAppConfig(customize=customize).customize == customize


@mark.parametrize("component_type", [_PlainDictComponent, _DictAnyComponent, _AnyComponent])
def test_per_instance_customize_for_untyped_component_passes(pulumi_mocks, component_type):
    component = component_type("c", customize={"bucket": {"force_destroy": True}})

    assert component._customizer("bucket", {}) == {"force_destroy": True}


@mark.skipif(sys.version_info < (3, 14), reason="needs lazy annotations")
def test_app_wide_customize_for_lazily_annotated_component_is_checked():
    class _CustomizationDict(TypedDict, total=False):
        bucket: Customization[BucketArgs]  # noqa: F821  # imported only under TYPE_CHECKING
        function: ChildCustomization[FunctionCustomizationDict]  # noqa: F821  # same

    class _LazyComponent(Component[_Res, _CustomizationDict]):
        def _create_resources(self) -> _Res:
            return _Res(1)

    customize = {_LazyComponent: {"bucket": {"force_destroy": True}}}
    assert StelvioAppConfig(customize=customize).customize == customize
    with raises(ValueError, match="unknown key 'bukcet'"):
        StelvioAppConfig(customize={_LazyComponent: {"bukcet": {}}})
    with raises(ValueError, match="'function' can't be customized app-wide"):
        StelvioAppConfig(customize={_LazyComponent: {"function": {}}})
