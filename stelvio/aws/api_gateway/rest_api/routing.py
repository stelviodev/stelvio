from stelvio.aws.api_gateway.rest_api.config import _ApiRoute
from stelvio.aws.api_gateway.routing import get_group_config_map, group_routes_by_handler


def _group_routes_by_lambda(routes: list[_ApiRoute]) -> dict[str, list[_ApiRoute]]:
    return group_routes_by_handler(routes)


def _get_group_config_map(grouped_routes: dict[str, list[_ApiRoute]]) -> dict[str, _ApiRoute]:
    return get_group_config_map(
        grouped_routes,
        multiple_configs_message="Multiple routes trying to configure the same lambda function",
    )
