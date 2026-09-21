from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack, final
from urllib.parse import quote_plus
from urllib.request import urlopen

from pulumi import Output
from pulumi_aws.docdb import Cluster, ClusterInstance, ClusterParameterGroup, SubnetGroup
from pulumi_aws.ec2 import SecurityGroup
from pulumi_aws.secretsmanager import SecretRotation, get_secret_version_output
from pulumi_aws.vpc import SecurityGroupIngressRule

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.aws.vpc import Vpc
from stelvio.component import Component, child_label, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig
from stelvio.project import get_dot_stelvio_dir
from stelvio.provider import ProviderStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pulumi_aws.docdb import (
        ClusterArgs,
        ClusterInstanceArgs,
        ClusterParameterGroupArgs,
        ClusterParameterGroupParameterArgs,
        SubnetGroupArgs,
    )
    from pulumi_aws.ec2 import SecurityGroupArgs

    from stelvio.customize import Customization

_ENGINE_VERSIONS: dict[str, str] = {"5.0": "5.0.0", "8.0": "8.0.0"}
_PARAMETER_FAMILIES: dict[str, str] = {"5.0": "docdb5.0", "8.0": "docdb8.0"}
_INSTANCE_CLASS_RE = re.compile(r"[a-z][a-z0-9]*\.[a-z0-9]+")
# AWS DocumentDB identifier rules: lowercase alphanumerics and single hyphens,
# first character a letter, no trailing hyphen.
_NAME_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_IDENTIFIER_UNSAFE_RE = re.compile(r"[^a-z0-9-]+")
_IDENTIFIER_HYPHENS_RE = re.compile(r"-{2,}")
_PULUMI_NAME_MAX_LENGTH = 255
_MIN_ISOLATED_SUBNETS = 2
_MAX_INSTANCES = 16
_MAX_SECRET_ROTATION_DAYS = 1000
_AWS_IDENTIFIER_MAX_LENGTH = 63
DOCDB_CA_PACKAGE_PATH = "stlv_docdb_ca.pem"
# Amazon RDS global CA bundle (DocumentDB uses the RDS trust store).
_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
_CA_BUNDLE_CACHE_RELATIVE_PATH = Path("aws") / "documentdb" / "global-bundle.pem"
_PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
_REPLICA_SET = "rs0"


@final
@dataclass(frozen=True)
class DocumentDbResources:
    """Pulumi resources created by a DocumentDb.

    `instances` is creation order: index 0 is the first instance.
    The AWS-managed master-user secret, ingress rule, and VPC app security
    group are not exposed.
    """

    cluster: Cluster
    instances: list[ClusterInstance]
    subnet_group: SubnetGroup
    parameter_group: ClusterParameterGroup
    security_group: SecurityGroup


class DocumentDbCustomizationDict(TypedDict, total=False):
    """Customization keys for DocumentDb resources. `instance` applies to every
    cluster instance — use a callable for per-instance values."""

    cluster: Customization[ClusterArgs]
    instance: Customization[ClusterInstanceArgs]
    subnet_group: Customization[SubnetGroupArgs]
    parameter_group: Customization[ClusterParameterGroupArgs]
    security_group: Customization[SecurityGroupArgs]


@dataclass(frozen=True, kw_only=True)
class DocumentDbConfig:
    """DocumentDB cluster configuration.

    Args:
        vpc: Existing Vpc whose isolated subnets host the cluster.
        instances: Number of cluster instances (default: 1).
        instance_class: Instance class with or without the ``db.`` prefix. None
            uses the app-wide default, falling back to ``t4g.medium``.
        engine: Engine version ``5.0`` or ``8.0`` (default: ``8.0``).
        deletion_protection: Block cluster deletion until flipped off. None uses
            the cluster default of False.
        backup_retention_period: Automated backup retention in days (1-35). None
            uses the cluster default of 7.
        secret_rotation: Automatic rotation interval for the AWS-managed master
            password in days, or ``False`` to disable rotation (default: 7).
    """

    vpc: Vpc
    instances: int = 1
    instance_class: str | None = None
    engine: Literal["5.0", "8.0"] = "8.0"
    deletion_protection: bool | None = None
    backup_retention_period: int | None = None
    secret_rotation: int | Literal[False] = 7

    def __post_init__(self) -> None:
        _validate_vpc(self.vpc)
        _validate_instances(self.instances)
        if self.instance_class is not None:
            object.__setattr__(
                self, "instance_class", _normalize_instance_class(self.instance_class)
            )
        _validate_engine(self.engine)
        if self.deletion_protection is not None:
            _validate_deletion_protection(self.deletion_protection)
        if self.backup_retention_period is not None:
            _validate_backup_retention_period(self.backup_retention_period)
        _validate_secret_rotation(self.secret_rotation)


class DocumentDbConfigDict(TypedDict, total=False):
    """Dict form of `DocumentDbConfig` — see it for field semantics."""

    vpc: Vpc
    instances: int
    instance_class: str | None
    engine: Literal["5.0", "8.0"]
    deletion_protection: bool | None
    backup_retention_period: int | None
    secret_rotation: int | Literal[False]


@final
class DocumentDb(Component[DocumentDbResources, DocumentDbCustomizationDict], LinkableMixin):
    """Amazon DocumentDB cluster in a Vpc's isolated subnets.

    Creates a TLS-required, encrypted cluster with an AWS-managed master
    password. Instances sit in isolated subnets. TCP 27017 is open from the
    Vpc's shared app security group.
    """

    _config: DocumentDbConfig

    def __init__(
        self,
        name: str,
        config: DocumentDbConfig | DocumentDbConfigDict | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: DocumentDbCustomizationDict | None = None,
        **opts: Unpack[DocumentDbConfigDict],
    ) -> None:
        super().__init__(
            ProviderStore.aws(), "stelvio:aws:DocumentDb", name, tags=tags, customize=customize
        )
        _validate_name(name)
        combining = config is not None and bool(opts)
        if not combining and not isinstance(config, DocumentDbConfig):
            mapping = opts if config is None else config
            if isinstance(mapping, dict) and "vpc" not in mapping:
                raise TypeError(f"DocumentDb '{name}' requires vpc=")
        self._config = self._parse_config(config, opts)
        if self._config.vpc.az_count < _MIN_ISOLATED_SUBNETS:
            raise ValueError(
                f"DocumentDb '{name}' requires a Vpc with at least {_MIN_ISOLATED_SUBNETS} "
                f"availability zones, got {self._config.vpc.az_count} from Vpc "
                f"{self._config.vpc.name!r}."
            )

    @staticmethod
    def _parse_config(
        config: DocumentDbConfig | DocumentDbConfigDict | None, opts: DocumentDbConfigDict
    ) -> DocumentDbConfig:
        if config is not None and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional options "
                "- provide all settings either in 'config' or as separate options"
            )
        if config is None:
            return DocumentDbConfig(**opts)
        if isinstance(config, DocumentDbConfig):
            return config
        if isinstance(config, dict):
            return DocumentDbConfig(**config)
        raise TypeError(
            f"Invalid config type: expected DocumentDbConfig or DocumentDbConfigDict, "
            f"got {type(config).__name__}"
        )

    @property
    def config(self) -> DocumentDbConfig:
        return self._config

    def _create_resources(self) -> DocumentDbResources:
        isolated = self.config.vpc.resources.isolated_subnets
        subnet_group_name = self._safe_name("-subnet-group")
        subnet_group = SubnetGroup(
            subnet_group_name,
            **self._customizer(
                "subnet_group",
                {
                    "subnet_ids": [subnet.id for subnet in isolated],
                    "tags": {"Name": subnet_group_name},
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        family = _PARAMETER_FAMILIES[self.config.engine]
        parameter_group_name = self._safe_name("-parameter-group")
        parameter_group_props = self._customizer(
            "parameter_group",
            {
                "family": family,
                "tags": {"Name": parameter_group_name},
            },
            default_props={"parameters": [{"name": "tls", "value": "enabled"}]},
            inject_tags=True,
        )
        # Customize is a shallow merge, so a parameters list replaces TLS. Put
        # tls=enabled back unless the user set tls themselves.
        parameter_group_props["parameters"] = Output.from_input(
            parameter_group_props.get("parameters")
        ).apply(_parameters_with_tls)
        parameter_group = ClusterParameterGroup(
            parameter_group_name,
            **parameter_group_props,
            opts=self._resource_opts(),
        )

        sg_name = self._safe_name("-sg")
        security_group = SecurityGroup(
            sg_name,
            **self._customizer(
                "security_group",
                {
                    "vpc_id": self.config.vpc.resources.vpc.id,
                    "tags": {"Name": sg_name},
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )
        # No egress: DocumentDB initiates no customer-visible outbound traffic, and an
        # empty egress set is valid. Ingress is a standalone rule from the Vpc app SG.

        cluster_name = self._safe_name(max_length=_AWS_IDENTIFIER_MAX_LENGTH)
        cluster = Cluster(
            cluster_name,
            **self._customizer(
                "cluster",
                {
                    "cluster_identifier": _aws_identifier(self.name),
                    "engine_version": _ENGINE_VERSIONS[self.config.engine],
                    "db_subnet_group_name": subnet_group.name,
                    "vpc_security_group_ids": [security_group.id],
                    "db_cluster_parameter_group_name": parameter_group.name,
                    "backup_retention_period": self.config.backup_retention_period,
                    "deletion_protection": self.config.deletion_protection,
                    "tags": {"Name": cluster_name},
                },
                default_props={
                    "engine": "docdb",
                    "master_username": "stelvio",
                    "manage_master_user_password": True,
                    "storage_encrypted": True,
                    "port": 27017,
                    "backup_retention_period": 7,
                    "skip_final_snapshot": True,
                    "deletion_protection": False,
                    # In-place `engine` bumps must opt in via customize; AWS rejects
                    # them unless this is already True on the live cluster.
                    "allow_major_version_upgrade": False,
                },
                inject_tags=True,
            ),
            # AWS pads unspecified AZs to 3; configuring 2 ForceNew-replaces the cluster
            # (hashicorp/terraform-provider-aws#19451, #37210). Omit the field and ignore
            # the pad so a default 2-AZ Vpc does not recreate the database.
            opts=self._resource_opts(ignore_changes=["availability_zones"]),
        )

        if self.config.secret_rotation is not False:
            SecretRotation(
                self._safe_name("-secret-rotation"),
                secret_id=cluster.master_user_secrets.apply(
                    lambda secrets: _master_secret_arn(self, secrets)
                ),
                rotation_rules={
                    "automatically_after_days": self.config.secret_rotation,
                },
                rotate_immediately=False,
                opts=self._resource_opts(),
            )

        # After the cluster so from_port/to_port follow cluster.port (including customize).
        app_sg = self.config.vpc.app_security_group
        SecurityGroupIngressRule(
            self._safe_name("-ingress"),
            security_group_id=security_group.id,
            referenced_security_group_id=app_sg.id,
            ip_protocol="tcp",
            from_port=cluster.port,
            to_port=cluster.port,
            tags=self.tags or None,
            opts=self._resource_opts(),
        )

        instance_class = (
            f"db.{self.config.instance_class}" if self.config.instance_class is not None else None
        )
        instances = []
        for i in range(1, self.config.instances + 1):
            instance_name = self._safe_name(f"-{i}", max_length=_AWS_IDENTIFIER_MAX_LENGTH)
            instances.append(
                ClusterInstance(
                    instance_name,
                    **self._customizer(
                        "instance",
                        {
                            "cluster_identifier": cluster.id,
                            "identifier": _aws_identifier(self.name, f"-{i}"),
                            "instance_class": instance_class,
                            "tags": {"Name": instance_name},
                        },
                        default_props={"engine": "docdb", "instance_class": "db.t4g.medium"},
                        inject_tags=True,
                    ),
                    opts=self._resource_opts(),
                )
            )

        return DocumentDbResources(
            cluster=cluster,
            instances=instances,
            subnet_group=subnet_group,
            parameter_group=parameter_group,
            security_group=security_group,
        )

    def _safe_name(
        self,
        suffix: str = "",
        *,
        max_length: int = _PULUMI_NAME_MAX_LENGTH,
        pulumi_suffix_length: int = 8,
    ) -> str:
        return safe_name(
            context().prefix(),
            self.name,
            max_length,
            suffix,
            pulumi_suffix_length=pulumi_suffix_length,
        )


@child_label("DocumentDb")
def _document_db_child_label(_name: str) -> str | None:
    """Keep the default suffix (`1`, `2` after stripping app, env and component)."""
    return None


def _validate_name(name: str) -> None:
    """Reject component names AWS would reject in DocumentDB identifiers."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"`name` must contain only lowercase letters, digits and single hyphens, start "
            f"with a letter and not end with a hyphen (AWS DocumentDB identifier rules), "
            f"got {name!r}"
        )


def _aws_identifier_prefix() -> str:
    return _IDENTIFIER_HYPHENS_RE.sub("-", _IDENTIFIER_UNSAFE_RE.sub("-", context().prefix()))


def _aws_identifier(name: str, suffix: str = "") -> str:
    raw = safe_name(
        _aws_identifier_prefix(), name, _AWS_IDENTIFIER_MAX_LENGTH, suffix, pulumi_suffix_length=0
    )
    return _IDENTIFIER_HYPHENS_RE.sub("-", raw).rstrip("-")


def _validate_vpc(vpc: Vpc) -> None:
    if not isinstance(vpc, Vpc):
        raise TypeError(f"`vpc` must be a Vpc instance, got {type(vpc).__name__}")


def _validate_instances(instances: int) -> None:
    if isinstance(instances, bool) or not isinstance(instances, int):
        raise TypeError(f"`instances` must be an int, got {type(instances).__name__}")
    if not 1 <= instances <= _MAX_INSTANCES:
        raise ValueError(f"`instances` must be between 1 and {_MAX_INSTANCES}, got {instances}")


def _normalize_instance_class(instance_class: str) -> str:
    if not isinstance(instance_class, str):
        raise TypeError(f"`instance_class` must be a str, got {type(instance_class).__name__}")
    if not instance_class:
        raise ValueError(f"`instance_class` must be a non-empty string, got {instance_class!r}")
    normalized = instance_class.removeprefix("db.")
    if not _INSTANCE_CLASS_RE.fullmatch(normalized):
        raise ValueError(
            f"`instance_class` must be family.size (e.g. 't4g.medium' or 'db.t4g.medium'), "
            f"got {instance_class!r}"
        )
    return normalized


def _parameter_name(parameter: object) -> str | None:
    if isinstance(parameter, dict):
        return parameter.get("name")
    return getattr(parameter, "name", None)


def _parameters_with_tls(
    parameters: Sequence[dict[str, str] | ClusterParameterGroupParameterArgs] | None,
) -> list[dict[str, str] | ClusterParameterGroupParameterArgs]:
    items = list(parameters) if parameters else []
    if any(_parameter_name(p) == "tls" for p in items):
        return items
    return [{"name": "tls", "value": "enabled"}, *items]


def _validate_engine(engine: str) -> None:
    if not isinstance(engine, str):
        raise TypeError(f"`engine` must be a str, got {type(engine).__name__}")
    if engine not in _ENGINE_VERSIONS:
        raise ValueError(f"`engine` must be '5.0' or '8.0', got {engine!r}")


def _validate_deletion_protection(deletion_protection: bool) -> None:
    if not isinstance(deletion_protection, bool):
        raise TypeError(
            f"`deletion_protection` must be a bool, got {type(deletion_protection).__name__}"
        )


def _validate_backup_retention_period(backup_retention_period: int) -> None:
    if isinstance(backup_retention_period, bool) or not isinstance(backup_retention_period, int):
        raise TypeError(
            f"`backup_retention_period` must be an int, "
            f"got {type(backup_retention_period).__name__}"
        )
    if not 1 <= backup_retention_period <= 35:  # noqa: PLR2004
        raise ValueError(
            f"`backup_retention_period` must be between 1 and 35, got {backup_retention_period}"
        )


def _validate_secret_rotation(secret_rotation: int | Literal[False]) -> None:
    if secret_rotation is False:
        return
    if isinstance(secret_rotation, bool) or not isinstance(secret_rotation, int):
        raise TypeError(
            f"`secret_rotation` must be False or an int, got {type(secret_rotation).__name__}"
        )
    if not 1 <= secret_rotation <= _MAX_SECRET_ROTATION_DAYS:
        raise ValueError(
            f"`secret_rotation` must be between 1 and {_MAX_SECRET_ROTATION_DAYS}, "
            f"or False, got {secret_rotation}"
        )


@link_config_creator(DocumentDb)
def default_document_db_link(document_db: DocumentDb) -> LinkConfig:
    """Default link: connection properties plus GetSecretValue on the managed secret.

    Auth is username/password; there are no DocumentDB data-plane IAM actions.
    """
    cluster = document_db.resources.cluster
    secret_arn = cluster.master_user_secrets.apply(
        lambda secrets: _master_secret_arn(document_db, secrets)
    )
    secret = get_secret_version_output(secret_id=secret_arn)
    connection_string = Output.secret(
        Output.all(
            cluster.endpoint, cluster.port, cluster.master_username, secret.secret_string
        ).apply(_mongo_connection_string_from_outputs)
    )
    return LinkConfig(
        properties={
            "host": cluster.endpoint,
            "reader_host": cluster.reader_endpoint,
            "port": cluster.port.apply(str),
            "username": cluster.master_username,
            "secret_arn": secret_arn,
            "replica_set": _REPLICA_SET,
            "ca_file": DOCDB_CA_PACKAGE_PATH,
            "connection_string": connection_string,
        },
        permissions=[
            AwsPermission(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret_arn],
            ),
        ],
    )


def _document_db_ca_path() -> Path:
    """Local path to the AWS global CA bundle, downloading into `.stelvio/` if needed.

    Called when packaging a Function that links DocumentDb (deploy/diff), not on import.
    """
    cache_path = get_dot_stelvio_dir() / _CA_BUNDLE_CACHE_RELATIVE_PATH
    if _ca_cache_valid(cache_path):
        return cache_path
    _download_document_db_ca(cache_path)
    return cache_path


def _ca_cache_valid(path: Path) -> bool:
    return path.is_file() and _PEM_BEGIN in path.read_bytes()


def _download_document_db_ca(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(_CA_BUNDLE_URL, timeout=30) as response:  # noqa: S310
            data = response.read()
    except OSError as exc:
        raise RuntimeError(
            f"Failed to download DocumentDB CA bundle from {_CA_BUNDLE_URL}: {exc}"
        ) from exc
    if not data or _PEM_BEGIN not in data:
        raise RuntimeError(
            f"DocumentDB CA bundle from {_CA_BUNDLE_URL} is empty or not a PEM file."
        )
    tmp = dest.with_name(f"{dest.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)


def _linked_document_dbs(links: Sequence[object]) -> list[DocumentDb]:
    found: list[DocumentDb] = []
    for item in links:
        if isinstance(item, DocumentDb):
            found.append(item)
        else:
            component = getattr(item, "component", None)
            if isinstance(component, DocumentDb):
                found.append(component)
    return found


def _validate_function_document_db_vpc(
    function_name: str, function_vpc: Vpc | None, links: Sequence[object]
) -> None:
    """Functions that link a DocumentDb must join the cluster's Vpc.

    Linking injects env vars and IAM; it does not create a network path.
    """
    for db in _linked_document_dbs(links):
        if function_vpc is None:
            raise ValueError(
                f"Function '{function_name}' links DocumentDb '{db.name}' but has no vpc=. "
                f"Set vpc= to the same Vpc as the cluster ({db.config.vpc.name!r}); "
                "linking is not networking."
            )
        if function_vpc is not db.config.vpc:
            raise ValueError(
                f"Function '{function_name}' links DocumentDb '{db.name}' in Vpc "
                f"{db.config.vpc.name!r} but is attached to Vpc {function_vpc.name!r}. "
                "Set vpc= to the same Vpc as the cluster; linking is not networking."
            )


def _mongo_connection_string_from_outputs(args: Sequence[object]) -> str:
    host, port, username, secret_string = args
    return _mongo_connection_string(
        host=str(host), port=port, username=str(username), secret_string=str(secret_string)
    )


def _mongo_connection_string(*, host: str, port: object, username: str, secret_string: str) -> str:
    password = json.loads(secret_string)["password"]
    user = quote_plus(username)
    pw = quote_plus(password)
    ca_file = quote_plus(DOCDB_CA_PACKAGE_PATH)
    return (
        f"mongodb://{user}:{pw}@{host}:{port}/"
        f"?tls=true&tlsCAFile={ca_file}&replicaSet={_REPLICA_SET}&retryWrites=false"
    )


def _master_secret_arn(document_db: DocumentDb, secrets: Sequence[object] | None) -> str:
    if secrets:
        secret = secrets[0]
        arn = getattr(secret, "secret_arn", None)
        if not arn and isinstance(secret, dict):
            arn = secret.get("secret_arn") or secret.get("secretArn")
        if arn:
            return arn
    raise ValueError(
        f"Cannot link DocumentDb {document_db.name!r}: the cluster has no AWS-managed "
        "master-user secret. Linking requires 'manage_master_user_password' to stay "
        "enabled (the default) - it was likely disabled via customize."
    )
