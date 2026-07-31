import pulumi

from stelvio.aws.api_gateway import RestApi


def when_api_ready(api: RestApi, callback):
    """Trigger callback after all API resources (including permissions) are created."""
    outputs = [api.resources.stage.id]
    outputs.extend(p.id for p in api._permissions)
    if api.resources.base_path_mapping is not None:
        outputs.append(api.resources.base_path_mapping.id)
    pulumi.Output.all(*outputs).apply(callback)
