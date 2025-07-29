import pulumi
import pulumi_cloudflare

from stelvio import dns


class CloudflarePulumiResourceAdapter(dns.Record):
    @property
    def name(self):
        return self._pulumi_resource.name

    @property
    def type(self):
        return self._pulumi_resource.type

    @property
    def value(self):
        return self._pulumi_resource.content


class CloudflareDns(dns.Dns):
    def __init__(self, zone_id: str):
        self.zone_id = zone_id

    def create_caa_record(self, resource_name, name, type, content, ttl=1) -> dns.Record:
        validation_record = pulumi_cloudflare.Record(
            resource_name, zone_id=self.zone_id, name=name, type=type, content=content, ttl=ttl
        )
        return CloudflarePulumiResourceAdapter(validation_record)

    def create_record(self, resource_name, name, dns_type, value, ttl=1) -> dns.Record:
        record = pulumi_cloudflare.Record(
            resource_name, zone_id=self.zone_id, name=name, type=dns_type, content=value, ttl=ttl
        )
        return CloudflarePulumiResourceAdapter(record)
