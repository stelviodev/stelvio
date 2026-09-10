from pulumi import Input, ResourceOptions
from pulumi_cloudflare import Record as CloudflareRecord

from stelvio.dns import Record


class CloudflareDns:
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
        record = CloudflareRecord(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            content=value,
            ttl=ttl,
            opts=opts,
        )
        return Record(record, record.name)
