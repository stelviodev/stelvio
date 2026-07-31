from enum import Enum
from typing import Literal


# These are methods supported by API Gateway.
class HTTPMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    ANY = "ANY"


HTTPMethodLiteral = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY", "*"]

type HTTPMethodInput = (
    str | HTTPMethodLiteral | HTTPMethod | list[str | HTTPMethodLiteral | HTTPMethod]
)


def normalize_method(method: str | HTTPMethodLiteral | HTTPMethod) -> str:
    if isinstance(method, HTTPMethod):
        return method.value
    method_upper = method.upper()
    return HTTPMethod.ANY.value if method_upper == "*" else method_upper


def validate_single_method(method: str | HTTPMethod, *, use_repr: bool = True) -> None:
    if isinstance(method, HTTPMethod):
        return
    method_upper = method.upper()
    if method_upper in ("ANY", "*"):
        return
    valid_methods = {value.value for value in HTTPMethod if value != HTTPMethod.ANY}
    if method_upper not in valid_methods:
        method_display = repr(method) if use_repr else method
        raise ValueError(f"Invalid HTTP method: {method_display}")


def validate_method_input(
    method: HTTPMethodInput,
    *,
    allow_any_in_list_message: str,
    use_repr_in_invalid_method: bool = True,
) -> None:
    if isinstance(method, str | HTTPMethod):
        validate_single_method(method, use_repr=use_repr_in_invalid_method)
        return

    if isinstance(method, list):
        if not method:
            raise ValueError("Method list cannot be empty")
        for item in method:
            if not isinstance(item, str | HTTPMethod):
                raise TypeError(f"Invalid method type in list: {type(item)}")
            if isinstance(item, str) and item.upper() in ("ANY", "*"):
                raise ValueError(allow_any_in_list_message)
            if isinstance(item, HTTPMethod) and item == HTTPMethod.ANY:
                raise ValueError("ANY is not allowed in a method list")
            validate_single_method(item, use_repr=use_repr_in_invalid_method)
        return

    raise TypeError(f"Method must be string, HTTPMethod, or list of them, got {type(method)}")
