"""Reusable inspection helpers for Stelvio project graphs and handler source."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.topic import Topic, TopicQueueSubscription, TopicSubscription
from stelvio.component import Component, ComponentRegistry
from stelvio.link import Link, Linkable


def components_of_type[T: Component](component_type: type[T]) -> list[T]:
    return list(ComponentRegistry.instances_of(component_type))


def find_dynamo_table(name: str) -> DynamoTable | None:
    for table in components_of_type(DynamoTable):
        if table.name == name:
            return table
    return None


def find_http_api(name: str | None = None) -> HttpApi | None:
    apis = components_of_type(HttpApi)
    if name is None:
        return apis[0] if apis else None
    for api in apis:
        if api.name == name:
            return api
    return None


def find_queue(name: str) -> Queue | None:
    for queue in components_of_type(Queue):
        if queue.name == name:
            return queue
    return None


def find_topic(name: str | None = None) -> Topic | None:
    topics = components_of_type(Topic)
    if name is None:
        return topics[0] if len(topics) == 1 else None
    for topic in topics:
        if topic.name == name:
            return topic
    return None


def find_bucket(name: str | None = None) -> Bucket | None:
    buckets = components_of_type(Bucket)
    if name is None:
        return buckets[0] if buckets else None
    for bucket in buckets:
        if bucket.name == name:
            return bucket
    return None


def case_root_for(project_dir: Path) -> Path:
    """Return ``cases/<id>/`` for a fixture or solution project directory."""
    project_dir = project_dir.resolve()
    if project_dir.name == "fixture":
        return project_dir.parent
    # .../cases/<id>/solutions/<name>
    return project_dir.parent.parent


def fixture_dir_for(project_dir: Path) -> Path:
    return case_root_for(project_dir) / "fixture"


def link_target_names(links: list[Link | Linkable]) -> set[str]:
    names: set[str] = set()
    for item in links:
        if isinstance(item, Link | Component):
            names.add(item.name)
        elif hasattr(item, "link"):
            names.add(item.link().name)
    return names


def route_matches(api: HttpApi, method: str, path: str) -> bool:
    method_u = method.upper()
    for route in api._routes:
        if path != route.path:
            continue
        if method_u in route.methods:
            return True
    return False


def routes_for(api: HttpApi, method: str, path: str) -> list:
    method_u = method.upper()
    return [route for route in api._routes if route.path == path and method_u in route.methods]


def handler_config_for_route(api: HttpApi, method: str, path: str) -> FunctionConfig | None:
    matched = routes_for(api, method, path)
    if not matched:
        return None
    handler = matched[0].handler
    if isinstance(handler, Function):
        return handler.config
    return handler


def handler_entrypoint(config: FunctionConfig) -> str:
    return config.handler


def resolve_handler_path(project_dir: Path, config: FunctionConfig) -> Path | None:
    """Map a FunctionConfig handler string to a source file under project_dir."""
    handler = config.handler
    if "::" in handler:
        folder, rest = handler.split("::", 1)
        file_part = rest.rsplit(".", 1)[0]
        candidate = project_dir / folder / f"{file_part}.py"
        return candidate if candidate.is_file() else None

    if config.folder:
        file_part = handler.rsplit(".", 1)[0]
        candidate = project_dir / config.folder / f"{file_part}.py"
        return candidate if candidate.is_file() else None

    file_part = handler.rsplit(".", 1)[0]
    candidate = project_dir / f"{file_part}.py"
    return candidate if candidate.is_file() else None


def read_handler_source(project_dir: Path, config: FunctionConfig) -> str | None:
    path = resolve_handler_path(project_dir, config)
    if path is None:
        return None
    return path.read_text(encoding="utf-8")


def source_uses_resources_attr(source: str, component_attr: str, property_name: str) -> bool:
    """True if source accesses Resources.<component_attr>.<property_name>."""
    pattern = rf"Resources\.{re.escape(component_attr)}\.{re.escape(property_name)}"
    return re.search(pattern, source) is not None


def source_imports_resources(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "stlv_resources" in source and "Resources" in source

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "stlv_resources":
            for alias in node.names:
                if alias.name in {"Resources", "*"}:
                    return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "stlv_resources":
                    return True
    return False


def source_reads_dynamo_item(source: str) -> bool:
    markers = (
        "get_item",
        "GetItem",
        "batch_get_item",
        "BatchGetItem",
    )
    return any(m in source for m in markers)


def source_writes_dynamo_item(source: str) -> bool:
    markers = ("put_item", "PutItem", "update_item", "UpdateItem")
    return any(m in source for m in markers)


def source_hardcodes_table_name(source: str, table_name: str) -> bool:
    """Heuristic: string literal equal to the logical table name used as a table id."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return f'"{table_name}"' in source or f"'{table_name}'" in source

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == table_name:
            return True
    return False


def source_calls_name(source: str, func_name: str) -> bool:
    """True if ``func_name(...)`` is called (definitions do not count)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(re.search(rf"(?<!def )\b{re.escape(func_name)}\s*\(", source))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == func_name:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr == func_name:
                return True
    return False


def function_source(source: str, func_name: str) -> str | None:
    """Return the source segment for a top-level function, if present."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(source, node)
    return None


def source_enqueues(source: str) -> bool:
    markers = ("send_message", "SendMessage", "send_messages", "queue_url", "queue_url")
    return any(m in source for m in markers) or source_uses_resources_attr(
        source, "orders", "queue_url"
    )


def source_publishes_sns(source: str) -> bool:
    markers = ("publish", "Publish")
    return any(m in source for m in markers)


def queue_subscriptions(queue: Queue) -> list[QueueSubscription]:
    return list(queue._subscriptions)


def topic_lambda_subscriptions(topic: Topic) -> list[TopicSubscription]:
    return list(topic._subscriptions)


def topic_queue_subscriptions(topic: Topic) -> list[TopicQueueSubscription]:
    return list(topic._queue_subscriptions)


def topic_consumer_count(topic: Topic) -> int:
    return len(topic._subscriptions) + len(topic._queue_subscriptions)


def bucket_notify_subscriptions(bucket: Bucket) -> list[BucketNotifySubscription]:
    return list(bucket._subscriptions)


def notify_is_function(sub: BucketNotifySubscription) -> bool:
    return sub._function_config is not None


def notify_is_queue(sub: BucketNotifySubscription) -> bool:
    return sub._queue is not None


def notify_handler_entrypoint(sub: BucketNotifySubscription) -> str | None:
    if sub._function_config is None:
        return None
    return sub._function_config.handler


def customize_queue_kms(queue: Queue) -> str | None:
    queue_custom = queue._customize.get("queue")
    if queue_custom is None:
        return None
    if callable(queue_custom):
        return None
    if isinstance(queue_custom, dict):
        value = queue_custom.get("kms_master_key_id")
        return value if isinstance(value, str) else None
    return None


def component_names_of_type[T: Component](component_type: type[T]) -> set[str]:
    return {c.name for c in components_of_type(component_type)}
