import pulumi_aws
from pulumi import Input, Output, ResourceOptions

from stelvio import dns


class Route53PulumiResourceAdapter(dns.Record):
    @property
    def name(self) -> Output[str]:
        return self.pulumi_resource.name

    @property
    def type(self) -> Output[str]:
        return self.pulumi_resource.type

    @property
    def value(self) -> Output[str]:
        return self.pulumi_resource.records.apply(lambda records: records[0])


class Route53Dns(dns.Dns):
    def __init__(self, zone_id: str):
        self.zone_id = zone_id

    def create_caa_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> dns.Record:
        validation_record = pulumi_aws.route53.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            records=[content],
            ttl=ttl,
            opts=opts,
        )
        return Route53PulumiResourceAdapter(validation_record)

    def create_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: str,
        record_type: str,
        value: Input[str],
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> dns.Record:
        record = pulumi_aws.route53.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            records=[value],
            ttl=ttl,
            opts=opts,
        )
        return Route53PulumiResourceAdapter(record)
