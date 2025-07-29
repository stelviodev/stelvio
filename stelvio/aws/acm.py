import pulumi
import pulumi_aws
from stelvio.dns import Dns


class AcmValidatedDomain:
    def __init__(self, name, domain_name: str, dns: "dns.Dns", prefix_fn: callable):
        self.name = name
        self.dns = dns
        self.domain_name = domain_name
        self.prefix_fn = prefix_fn
        self.pulumi_resources = []
        self.certificate = None
        self.validation_record = None
        self.cert_validation = None

    def create(self):
        # 1 - Issue Certificate
        self.certificate = pulumi_aws.acm.Certificate(
            self.prefix_fn(f"{self.name}-custom-domain-certificate"),
            domain_name=self.domain_name,
            validation_method="DNS",
        )

        # 2 - Validate Certificate with DNS PROVIDER
        self.validation_record = self.dns.create_caa_record(
            resource_name=f"{self.prefix_fn(f'{self.name}-custom-domain-certificate-validation-record')}",
            name=self.certificate.domain_validation_options[0].resource_record_name,
            type=self.certificate.domain_validation_options[0].resource_record_type,
            content=self.certificate.domain_validation_options[0].resource_record_value,
            ttl=1,
        )

        # 3 - Wait for validation - use the validation record's FQDN to ensure it exists
        self.cert_validation = pulumi_aws.acm.CertificateValidation(
            self.prefix_fn(f"{self.name}-custom-domain-certificate-validation"),
            certificate_arn=self.certificate.arn,
            validation_record_fqdns=[
                self.validation_record.name
            ],  # This ensures validation_record exists
            opts=pulumi.ResourceOptions(
                depends_on=[self.certificate, self.validation_record._pulumi_resource]
            ),
        )

        self.pulumi_resources.append(self.certificate)
        self.pulumi_resources.append(self.validation_record)
        self.pulumi_resources.append(self.cert_validation)
