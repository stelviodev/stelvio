import pulumi_aws
from pulumi import Input, ResourceOptions

from stelvio.dns import Record


class Route53Dns:
    def __init__(self, zone_id: str):
        self.zone_id = zone_id

    def create_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: Input[str],
        record_type: Input[str],
        value: Input[str],
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record:
        record = pulumi_aws.route53.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            records=[value],
            ttl=ttl,
            opts=opts,
        )
        return Record(record, record.name)
