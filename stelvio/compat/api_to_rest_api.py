"""Prepare stacks for the Api → RestApi rename.

Removes custom domains and unmanaged access-log groups that would collide
when RestApi starts managing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

import boto3
import click
from botocore.exceptions import ClientError
from rich.console import Console

from stelvio.command_run import CommandRun
from stelvio.context import context
from stelvio.state_ops import list_resources, remove_resource

if TYPE_CHECKING:
    from collections.abc import Callable

console = Console()

API_COMPONENT_TYPES = frozenset({"stelvio:aws:Api", "stelvio:aws:RestApi"})
REST_API_TYPE = "aws:apigateway/restApi:RestApi"
DOMAIN_TYPE = "aws:apigateway/domainName:DomainName"
BASE_PATH_MAPPING_TYPE = "aws:apigateway/basePathMapping:BasePathMapping"
LOG_GROUP_TYPE = "aws:cloudwatch/logGroup:LogGroup"
ROUTE53_RECORD_TYPE = "aws:route53/record:Record"


class ActionKind(StrEnum):
    DELETE_DOMAIN = "delete_domain"
    DELETE_LOG_GROUP = "delete_log_group"
    STATE_RM = "state_rm"


@dataclass(frozen=True)
class CompatAction:
    kind: ActionKind
    label: str
    api_component: str
    domain_name: str | None = None
    log_group_name: str | None = None
    urn: str | None = None


@dataclass(frozen=True)
class ActionResult:
    action: CompatAction
    status: Literal["done", "skipped", "failed"]
    detail: str = ""


class ApiGatewayClient(Protocol):
    def delete_domain_name(self, *, domainName: str) -> object:  # noqa: N803
        ...


class LogsClient(Protocol):
    def delete_log_group(self, *, logGroupName: str) -> object:  # noqa: N803
        ...

    def describe_log_groups(
        self,
        *,
        logGroupNamePrefix: str,  # noqa: N803
        limit: int,
    ) -> dict[str, list[dict[str, str]]]: ...


@dataclass(frozen=True)
class AwsClients:
    apigateway: ApiGatewayClient
    logs: LogsClient


def _raw_resources(state: dict) -> list[dict]:
    return state.get("checkpoint", {}).get("latest", {}).get("resources", [])


def _attr(raw: dict, *keys: str) -> str | None:
    for bag_name in ("outputs", "inputs"):
        bag = raw.get(bag_name) or {}
        for key in keys:
            value = bag.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _children_of(raw_resources: list[dict], parent_urn: str) -> list[dict]:
    return [r for r in raw_resources if r.get("parent") == parent_urn]


def _expected_log_group_name(rest_api_name: str) -> str:
    return f"/aws/apigateway/{rest_api_name}"


def plan_actions(
    state: dict,
    *,
    log_group_exists: Callable[[str], bool],
) -> list[CompatAction]:
    """Build ordered actions to prepare Api/RestApi components for upgrade."""
    raw_resources = _raw_resources(state)
    actions: list[CompatAction] = []

    for component in list_resources(state):
        if component.type not in API_COMPONENT_TYPES:
            continue

        children = _children_of(raw_resources, component.urn)
        rest_api = next((c for c in children if c.get("type") == REST_API_TYPE), None)
        domain = next((c for c in children if c.get("type") == DOMAIN_TYPE), None)
        mappings = [c for c in children if c.get("type") == BASE_PATH_MAPPING_TYPE]
        log_groups = [c for c in children if c.get("type") == LOG_GROUP_TYPE]
        dns_records = [
            c
            for c in children
            if c.get("type") == ROUTE53_RECORD_TYPE and "custom-domain" in c.get("urn", "")
        ]

        if domain is not None:
            domain_name = _attr(domain, "domainName", "domain_name")
            if domain_name:
                actions.append(
                    CompatAction(
                        kind=ActionKind.DELETE_DOMAIN,
                        label=f"Delete API Gateway domain '{domain_name}' "
                        f"(API '{component.name}')",
                        api_component=component.name,
                        domain_name=domain_name,
                    )
                )
                for raw in (domain, *mappings, *dns_records):
                    urn = raw["urn"]
                    actions.append(
                        CompatAction(
                            kind=ActionKind.STATE_RM,
                            label=f"Remove from state: {raw['type']} '{urn.split('::')[-1]}'",
                            api_component=component.name,
                            urn=urn,
                        )
                    )

        if not log_groups and rest_api is not None:
            rest_api_name = _attr(rest_api, "name", "id")
            if rest_api_name:
                log_group_name = _expected_log_group_name(rest_api_name)
                if log_group_exists(log_group_name):
                    actions.append(
                        CompatAction(
                            kind=ActionKind.DELETE_LOG_GROUP,
                            label=(
                                f"Delete CloudWatch log group '{log_group_name}' "
                                f"(API '{component.name}'; historical logs in this group "
                                "will be removed)"
                            ),
                            api_component=component.name,
                            log_group_name=log_group_name,
                        )
                    )

    return actions


def _is_not_found(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code", "")
    return code in {
        "NotFoundException",
        "ResourceNotFoundException",
        "NotFound",
    }


def delete_domain(apigateway: ApiGatewayClient, domain_name: str) -> str:
    try:
        apigateway.delete_domain_name(domainName=domain_name)
    except ClientError as e:
        if _is_not_found(e):
            return f"Domain '{domain_name}' already absent in AWS"
        raise
    return f"Deleted domain '{domain_name}'"


def delete_log_group(logs: LogsClient, log_group_name: str) -> str:
    try:
        logs.delete_log_group(logGroupName=log_group_name)
    except ClientError as e:
        if _is_not_found(e):
            return f"Log group '{log_group_name}' already absent in AWS"
        raise
    return f"Deleted log group '{log_group_name}'"


def log_group_exists(logs: LogsClient, log_group_name: str) -> bool:
    try:
        response = logs.describe_log_groups(logGroupNamePrefix=log_group_name, limit=50)
    except ClientError:
        return False
    return any(g.get("logGroupName") == log_group_name for g in response.get("logGroups", []))


def _apply_action(action: CompatAction, state: dict, clients: AwsClients) -> str:
    if action.kind == ActionKind.DELETE_DOMAIN:
        if action.domain_name is None:
            raise ValueError("delete_domain action missing domain_name")
        return delete_domain(clients.apigateway, action.domain_name)

    if action.kind == ActionKind.DELETE_LOG_GROUP:
        if action.log_group_name is None:
            raise ValueError("delete_log_group action missing log_group_name")
        return delete_log_group(clients.logs, action.log_group_name)

    if action.kind == ActionKind.STATE_RM:
        if action.urn is None:
            raise ValueError("state_rm action missing urn")
        existing = {r.urn for r in list_resources(state)}
        if action.urn not in existing:
            return f"Already absent from state: {action.urn.split('::')[-1]}"
        mutations = remove_resource(state, action.urn)
        return "; ".join(m.detail for m in mutations) or "Removed from state"

    raise ValueError(f"Unknown action kind: {action.kind}")


def execute_actions(
    actions: list[CompatAction],
    state: dict,
    clients: AwsClients,
    *,
    interactive: bool,
    confirm: Callable[[str], bool] | None = None,
) -> list[ActionResult]:
    """Apply planned actions. Mutates state in place for STATE_RM."""
    confirm_fn = confirm or click.confirm
    results: list[ActionResult] = []

    for action in actions:
        if interactive and not confirm_fn(f"Run: {action.label}?"):
            results.append(ActionResult(action=action, status="skipped", detail="Declined"))
            continue
        try:
            detail = _apply_action(action, state, clients)
        except (ClientError, ValueError, TypeError, RuntimeError) as e:
            results.append(ActionResult(action=action, status="failed", detail=str(e)))
        else:
            results.append(ActionResult(action=action, status="done", detail=detail))

    return results


def _print_results(results: list[ActionResult]) -> int:
    failed = [r for r in results if r.status == "failed"]
    done = [r for r in results if r.status == "done"]
    skipped = [r for r in results if r.status == "skipped"]

    for result in results:
        if result.status == "done":
            console.print(f"[green]✓[/green] {result.action.label}")
            if result.detail:
                console.print(f"    {result.detail}")
        elif result.status == "skipped":
            console.print(f"[yellow]-[/yellow] Skipped: {result.action.label}")
        else:
            console.print(f"[red]✗[/red] {result.action.label}")
            console.print(f"    {result.detail}")

    console.print()
    console.print(
        f"[bold]Done.[/bold] {len(done)} applied, {len(skipped)} skipped, {len(failed)} failed."
    )
    if not failed:
        console.print("Next step: [bold]stlv deploy[/bold]")
    return 1 if failed else 0


def run_compat_api_to_rest_api(env: str, *, interactive: bool = False) -> int:
    """CLI entry: plan and apply compat actions. Returns process exit code."""
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="compat-api-to-rest-api", state_only=True) as run:
        status.stop()
        if not run.has_deployed:
            console.print("[yellow]No app deployed yet. Nothing to migrate.[/yellow]")
            run.complete_update()
            return 0

        state = run.load_state()
        if not state:
            console.print("[yellow]No state found. Nothing to migrate.[/yellow]")
            run.complete_update()
            return 0

        ctx = context()
        session = boto3.Session(profile_name=ctx.aws.profile, region_name=ctx.aws.region)
        clients = AwsClients(
            apigateway=session.client("apigateway"),
            logs=session.client("logs"),
        )

        actions = plan_actions(
            state,
            log_group_exists=lambda name: log_group_exists(clients.logs, name),
        )
        if not actions:
            console.print(
                "[green]✓ No Api/RestApi domain or unmanaged log-group cleanup needed.[/green]"
            )
            run.complete_update()
            return 0

        console.print(f"[bold]Planned {len(actions)} action(s):[/bold]")
        for action in actions:
            console.print(f"  • {action.label}")
        console.print()

        results = execute_actions(actions, state, clients, interactive=interactive)
        if any(r.status == "done" and r.action.kind == ActionKind.STATE_RM for r in results):
            run.push_state(state)

        failed = [r for r in results if r.status == "failed"]
        run.complete_update(errors=[r.detail for r in failed] or None)
        return _print_results(results)
