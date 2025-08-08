import pulumi_aws
from pulumi import Input, Output

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
        return self.pulumi_resource.content


class Route53Dns(dns.Dns):
    def __init__(self, zone_id: str):
        self.zone_id = zone_id

    def create_caa_record(
        self, resource_name: str, name: str, record_type: str, content: str, ttl: int = 1
    ) -> dns.Record:
        validation_record = pulumi_aws.route53.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            records=[content],
            ttl=ttl,
        )
        return Route53PulumiResourceAdapter(validation_record)

    def create_record(
        self, resource_name: str, name: str, record_type: str, value: Input[str], ttl: int = 1
    ) -> dns.Record:
        record = pulumi_aws.route53.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            records=[value],
            ttl=ttl,
        )
        return Route53PulumiResourceAdapter(record)
