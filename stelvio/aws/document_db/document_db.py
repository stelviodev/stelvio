from __future__ import annotations

import functools
import os
import re
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack, final
from urllib.parse import quote_plus
from urllib.request import urlopen

from pulumi import Output, ResourceOptions, log
from pulumi_aws.docdb import Cluster, ClusterInstance, ClusterParameterGroup, SubnetGroup
from pulumi_aws.ec2 import SecurityGroup
from pulumi_aws.secretsmanager import SecretRotation
from pulumi_aws.vpc import SecurityGroupIngressRule

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.aws.vpc import Vpc
from stelvio.component import (
    Component,
    link_config_creator,
    parse_config,
    resource_name,
)
from stelvio.link import LinkableMixin, LinkConfig
from stelvio.project import get_dot_stelvio_dir
from stelvio.provider import ProviderStore
from stelvio.tunnel.manifest import OUTPUT_KEY, VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pulumi import Input
    from pulumi_aws.docdb import (
        ClusterArgs,
        ClusterInstanceArgs,
        ClusterParameterGroupArgs,
        SubnetGroupArgs,
    )
    from pulumi_aws.ec2 import SecurityGroupArgs
    from pulumi_aws.secretsmanager import SecretRotationArgs

    from stelvio.customize import Customization

_ENGINE_VERSIONS: dict[str, str] = {"5.0": "5.0.0", "8.0": "8.0.0"}
_PARAMETER_FAMILIES: dict[str, str] = {"5.0": "docdb5.0", "8.0": "docdb8.0"}
_INSTANCE_CLASS_RE = re.compile(r"[a-z][a-z0-9]*\.[a-z0-9]+")
# AWS DocumentDB identifier rules: lowercase alphanumerics and single hyphens,
# first character a letter, no trailing hyphen.
_NAME_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
# Truncation can leave `--` where the cut lands on a hyphen. DocumentDB rejects that.
_CONSECUTIVE_HYPHENS_RE = re.compile(r"-{2,}")
_AWS_NAME_MAX_LENGTH = 255
_AWS_IDENTIFIER_MAX_LENGTH = 63
_DOCDB_GENERATED_SUFFIX_LENGTH = 26
_MIN_ISOLATED_SUBNETS = 2
_MAX_INSTANCES = 16
_MAX_SECRET_ROTATION_DAYS = 1000
_MIN_BACKUP_RETENTION_DAYS = 1
_MAX_BACKUP_RETENTION_DAYS = 35
_DOCDB_CA_PACKAGE_PATH = "stlv_docdb_ca.pem"
# Amazon RDS global CA bundle (DocumentDB uses the RDS trust store).
_DOCDB_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
_DOCDB_CA_CACHE_RELATIVE_PATH = Path("aws") / "documentdb" / "global-bundle.pem"
_DOCDB_CA_CACHE_TTL_SECONDS = 24 * 60 * 60
_DOCDB_CA_DOWNLOAD_TIMEOUT_SECONDS = 30
_DOCDB_CA_PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
_DOCDB_CA_PEM_END = b"-----END CERTIFICATE-----"
_DOCDB_SG_DESCRIPTION = "Stelvio DocumentDB: accepts connections from the VPC app security group."
_REPLICA_SET = "rs0"


@final
@dataclass(frozen=True)
class DocumentDbResources:
    """Pulumi resources created by a DocumentDb.

    The AWS-managed master-user secret, instances, subnet/parameter groups,
    ingress rule, and VPC app security group are not exposed.
    """

    cluster: Cluster
    security_group: SecurityGroup


class DocumentDbCustomizationDict(TypedDict, total=False):
    """Customization keys for DocumentDb resources. `instance` applies to every
    cluster instance — use a callable for per-instance values."""

    cluster: Customization[ClusterArgs]
    instance: Customization[ClusterInstanceArgs]
    subnet_group: Customization[SubnetGroupArgs]
    parameter_group: Customization[ClusterParameterGroupArgs]
    security_group: Customization[SecurityGroupArgs]
    secret_rotation: Customization[SecretRotationArgs]


@dataclass(frozen=True, kw_only=True)
class DocumentDbConfig:
    """DocumentDB cluster configuration.

    Args:
        vpc: Existing Vpc whose isolated subnets host the cluster.
        instances: Number of cluster instances (default: 1).
        instance_class: Instance class with or without the ``db.`` prefix. None
            uses ``t3.medium``.
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
    password. Instances sit in isolated subnets. The cluster port (default
    27017) is open from the Vpc's shared app security group.
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
        self._config = parse_config(DocumentDbConfig, config, opts)

    @property
    def config(self) -> DocumentDbConfig:
        return self._config

    @property
    def _link_vpc(self) -> Vpc:
        return self._config.vpc

    def _create_resources(self) -> DocumentDbResources:
        subnet_group = self._create_subnet_group()
        parameter_group = self._create_parameter_group()
        security_group = self._create_security_group()
        cluster, manage_password = self._create_cluster(
            subnet_group, parameter_group, security_group
        )
        instances = self._create_instances(cluster)
        if manage_password:
            self._create_secret_rotation(cluster, instances)
        self.register_outputs(
            {
                OUTPUT_KEY: {
                    "version": VERSION,
                    "kind": "resource",
                    "identity": self.urn,
                    "vpc": self.config.vpc.urn,
                    "service": "documentdb",
                    "resource_id": cluster.id,
                    "ports": [cluster.port],
                    "hostnames": [cluster.endpoint, cluster.reader_endpoint],
                    "security_groups": cluster.vpc_security_group_ids,
                }
            }
        )
        return DocumentDbResources(cluster=cluster, security_group=security_group)

    def _create_subnet_group(self) -> SubnetGroup:
        isolated = self.config.vpc.resources.isolated_subnets
        subnet_group_name = self._resource_name("-subnet-group")
        return SubnetGroup(
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

    def _create_parameter_group(self) -> ClusterParameterGroup:
        family = _PARAMETER_FAMILIES[self.config.engine]
        parameter_group_name = self._resource_name("-parameter-group")
        return ClusterParameterGroup(
            parameter_group_name,
            **self._customizer(
                "parameter_group",
                {
                    "family": family,
                    "tags": {"Name": parameter_group_name},
                },
                default_props={"parameters": [{"name": "tls", "value": "enabled"}]},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

    def _create_security_group(self) -> SecurityGroup:
        sg_name = self._resource_name("-sg")
        return SecurityGroup(
            sg_name,
            **self._customizer(
                "security_group",
                {
                    "vpc_id": self.config.vpc.resources.vpc.id,
                    "description": _DOCDB_SG_DESCRIPTION,
                    "tags": {"Name": sg_name},
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

    def _create_cluster(
        self,
        subnet_group: SubnetGroup,
        parameter_group: ClusterParameterGroup,
        security_group: SecurityGroup,
    ) -> tuple[Cluster, bool]:
        cluster_name = self._resource_name()
        cluster_props = self._customizer(
            "cluster",
            {
                "cluster_identifier_prefix": _identifier_prefix(self.name, suffix="-"),
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
        )
        # The provider rejects both forms together, so normalize before splatting.
        _prefer_explicit_identifier(cluster_props, "cluster_identifier_prefix")
        _validate_manage_master_user_password(cluster_props.get("manage_master_user_password"))
        cluster = Cluster(
            cluster_name,
            **cluster_props,
            # AWS pads unspecified AZs to 3; configuring 2 ForceNew-replaces the cluster
            # (hashicorp/terraform-provider-aws#19451, #37210). Omit the field and ignore
            # the pad so a default 2-AZ Vpc does not recreate the database.
            opts=ResourceOptions.merge(
                self._resource_opts(),
                ResourceOptions(ignore_changes=["availability_zones"]),
            ),
        )
        # After the cluster so from_port/to_port follow cluster.port (including customize).
        app_sg = self.config.vpc._app_security_group  # noqa: SLF001
        SecurityGroupIngressRule(
            self._resource_name("-ingress"),
            security_group_id=security_group.id,
            referenced_security_group_id=app_sg.id,
            ip_protocol="tcp",
            from_port=cluster.port,
            to_port=cluster.port,
            tags=self.tags or None,
            opts=self._resource_opts(),
        )
        bastion_group = self.config.vpc.resources.bastion_security_group
        if bastion_group is not None:
            # Keep resource topology visible during preview: member IDs can be
            # unknown Outputs, but the attachment list determines rule count.
            # This dedicated source group is owned by the VPC component.
            targets = cluster_props["vpc_security_group_ids"]
            if isinstance(targets, Output):
                log.warn(
                    f"DocumentDb {self.name}: customized security group membership is an "
                    "Output. Bastion ingress changes appear after membership resolves; "
                    "pass a list of security group IDs or individual Outputs to include "
                    "all ingress rules in preview.",
                    self,
                )
                targets.apply(
                    lambda groups: self._create_bastion_ingress(cluster, groups, bastion_group)
                )
            else:
                self._create_bastion_ingress(cluster, targets, bastion_group)
        return cluster, cluster_props.get("manage_master_user_password") is True

    def _create_bastion_ingress(
        self, cluster: Cluster, targets: Sequence[Input[str]], source: SecurityGroup
    ) -> None:
        for index, target in enumerate(dict.fromkeys(targets)):
            SecurityGroupIngressRule(
                self._resource_name(f"-bastion-ingress-{index}-0"),
                security_group_id=target,
                referenced_security_group_id=source.id,
                ip_protocol="tcp",
                from_port=cluster.port,
                to_port=cluster.port,
                tags=self.tags or None,
                opts=self._resource_opts(),
            )

    def _create_instances(self, cluster: Cluster) -> list[ClusterInstance]:
        instance_class = (
            f"db.{self.config.instance_class}" if self.config.instance_class is not None else None
        )
        instances: list[ClusterInstance] = []
        for i in range(1, self.config.instances + 1):
            instance_name = self._resource_name(f"-{i}")
            instance_props = self._customizer(
                "instance",
                {
                    "cluster_identifier": cluster.id,
                    "identifier_prefix": _identifier_prefix(self.name, suffix=f"-{i}-"),
                    "instance_class": instance_class,
                    "tags": {"Name": instance_name},
                },
                default_props={"engine": "docdb", "instance_class": "db.t3.medium"},
                inject_tags=True,
            )
            # The provider rejects both forms together, so normalize before splatting.
            _prefer_explicit_identifier(instance_props, "identifier_prefix")
            instances.append(
                ClusterInstance(
                    instance_name,
                    **instance_props,
                    opts=self._resource_opts(),
                )
            )
        return instances

    def _create_secret_rotation(self, cluster: Cluster, instances: list[ClusterInstance]) -> None:
        secret_id = cluster.master_user_secrets.apply(
            lambda secrets: _master_secret_arn(self, secrets)
        )
        rotation_props: dict[str, object] = {
            "secret_id": secret_id,
            "rotate_immediately": False,
        }
        if self.config.secret_rotation is False:
            rotation_props["rotation_enabled"] = False
        else:
            rotation_props["rotation_enabled"] = True
            rotation_props["rotation_rules"] = {
                "automatically_after_days": self.config.secret_rotation,
            }
        SecretRotation(
            self._resource_name("-secret-rotation"),
            **self._customizer("secret_rotation", rotation_props),
            # AWS re-enables rotation if this runs before the instances exist.
            opts=self._resource_opts(depends_on=instances),
        )

    def _resource_name(self, suffix: str = "") -> str:
        return resource_name(self.name, limit=_AWS_NAME_MAX_LENGTH, suffix=suffix)


def _validate_name(name: str) -> None:
    """Reject component names AWS would reject in DocumentDB identifiers."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"`name` must contain only lowercase letters, digits and single hyphens, start "
            f"with a letter and not end with a hyphen (AWS DocumentDB identifier rules), "
            f"got {name!r}"
        )


def _identifier_prefix(name: str, *, suffix: str) -> str:
    """DocumentDB identifier prefix, with consecutive hyphens collapsed.

    ``resource_name`` can emit ``--`` when truncation cuts on a hyphen.
    DocumentDB rejects that. Collapse only here: doing it in ``resource_name``
    would rename truncated names of other components.
    """
    raw = resource_name(
        name,
        limit=_AWS_IDENTIFIER_MAX_LENGTH,
        suffix=suffix,
        pulumi_suffix_length=_DOCDB_GENERATED_SUFFIX_LENGTH,
    )
    return _CONSECUTIVE_HYPHENS_RE.sub("-", raw)


def _prefer_explicit_identifier(props: dict[str, object], prefix_key: str) -> None:
    """Resolve the provider's mutually exclusive identifier and prefix inputs."""
    identifier_key = prefix_key.removesuffix("_prefix")
    if props.get(identifier_key) is not None:
        props.pop(prefix_key, None)
    else:
        props.pop(identifier_key, None)


def _validate_manage_master_user_password(value: object) -> None:
    if isinstance(value, bool):
        return
    raise ValueError(
        "'manage_master_user_password' must be a plain bool "
        "(Output and other deferred values are not supported), "
        f"got {type(value).__name__}: {value!r}."
    )


def _validate_vpc(vpc: Vpc) -> None:
    if not isinstance(vpc, Vpc):
        raise TypeError(f"`vpc` must be a Vpc instance, got {type(vpc).__name__}")
    az_count = vpc._az_count  # noqa: SLF001
    if az_count < _MIN_ISOLATED_SUBNETS:
        raise ValueError(
            f"`vpc` must have at least {_MIN_ISOLATED_SUBNETS} availability zones, "
            f"got {az_count} from Vpc {vpc.name!r}."
        )


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
            f"`instance_class` must be family.size (e.g. 't3.medium' or 'db.t3.medium'), "
            f"got {instance_class!r}"
        )
    return normalized


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
    if not _MIN_BACKUP_RETENTION_DAYS <= backup_retention_period <= _MAX_BACKUP_RETENTION_DAYS:
        raise ValueError(
            f"`backup_retention_period` must be between {_MIN_BACKUP_RETENTION_DAYS} and "
            f"{_MAX_BACKUP_RETENTION_DAYS}, got {backup_retention_period}"
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
    ca_cache_path = _document_db_ca_path(get_dot_stelvio_dir())
    # Deploy: package-relative path inside the Lambda zip. Dev: absolute cache path so
    # the local handler can open the real file without staging into cwd.
    ca_runtime_path = str(ca_cache_path) if context().dev_mode else _DOCDB_CA_PACKAGE_PATH
    # host+port only: including the secret would mark this URI secret. TLS is always on
    # in the default URI; intentionally disabling TLS needs a link-property override.
    connection_uri = Output.all(cluster.endpoint, cluster.port).apply(
        lambda args: _mongo_uri(host=str(args[0]), port=args[1], ca_file=ca_runtime_path)
    )
    return LinkConfig(
        properties={
            "host": cluster.endpoint,
            "reader_host": cluster.reader_endpoint,
            "port": cluster.port.apply(str),
            "username": cluster.master_username,
            "secret_arn": secret_arn,
            "replica_set": _REPLICA_SET,
            "ca_file": ca_runtime_path,
            "connection_uri": connection_uri,
        },
        permissions=[
            AwsPermission(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret_arn],
            ),
        ],
        _files={_DOCDB_CA_PACKAGE_PATH: ca_cache_path},
    )


@functools.cache
def _document_db_ca_path(dot_stelvio: Path) -> Path:
    """Absolute path to Amazon's global CA bundle, cached under `dot_stelvio`.

    Resolved once per process per project, when the link creator first runs;
    never at import or Function construction. `stlv dev` re-runs link creators
    on every local invocation, where a refresh would block the event loop and a
    failed download would stop the dev server. Failures are not memoized.
    """
    cache_path = (dot_stelvio / _DOCDB_CA_CACHE_RELATIVE_PATH).resolve()
    if not _ca_cache_valid(cache_path):
        _download_document_db_ca(cache_path)
    return cache_path


def _is_pem_bundle(data: bytes) -> bool:
    return _DOCDB_CA_PEM_BEGIN in data and data.rstrip().endswith(_DOCDB_CA_PEM_END)


def _ca_cache_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        if time.time() - path.stat().st_mtime > _DOCDB_CA_CACHE_TTL_SECONDS:
            return False
        return _is_pem_bundle(path.read_bytes())
    except OSError:
        return False


def _download_document_db_ca(dest: Path) -> None:
    try:
        # The endpoint is a fixed Amazon trust-store URL, not user input.
        with urlopen(_DOCDB_CA_URL, timeout=_DOCDB_CA_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
            data = response.read()
    except (OSError, HTTPException) as exc:
        raise RuntimeError(
            f"Failed to download DocumentDB CA bundle from {_DOCDB_CA_URL}: {exc}"
        ) from exc
    if not _is_pem_bundle(data):
        raise RuntimeError(
            f"DocumentDB CA bundle from {_DOCDB_CA_URL} is empty or not a PEM file."
        )
    _write_ca_cache(dest, data)


def _write_ca_cache(dest: Path, data: bytes) -> None:
    # Unique temp name + atomic replace: concurrent stlv runs share the cache file,
    # and a failed write must never clobber a previously cached bundle.
    tmp: Path | None = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name, suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp.replace(dest)
    except OSError as exc:
        raise RuntimeError(f"Failed to write DocumentDB CA bundle cache {dest}: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _mongo_uri(*, host: str, port: object, ca_file: str = _DOCDB_CA_PACKAGE_PATH) -> str:
    return (
        f"mongodb://{host}:{port}/?"
        f"tls=true&tlsCAFile={quote_plus(ca_file)}"
        f"&replicaSet={_REPLICA_SET}&retryWrites=false"
    )


def _master_secret_arn(document_db: DocumentDb, secrets: Sequence[object] | None) -> str:
    if secrets:
        return secrets[0].secret_arn
    raise ValueError(
        f"Cannot link DocumentDb {document_db.name!r}: the cluster has no AWS-managed "
        "master-user secret. Linking requires 'manage_master_user_password' to stay "
        "enabled (the default) - it was likely disabled via customize."
    )
