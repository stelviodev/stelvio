# Working With DNS in Stelvio

DNS (Domain Name System) is a crucial part of web infrastructure, translating human-readable domain names into IP addresses. Stelvio provides tools to manage DNS records.

In general, resources provided by various cloud providers usually have a DNS name that can be used to access them. However, you might want to use your own domain name instead of the default one provided by the cloud provider.

For example, you might want to use `api.example.com` instead of `api-1234567890.us-east-1.elb.amazonaws.com`. Stelvio makes it easy to set up custom domain names for your applications.

In modern applications, these custom domain names are most likely used as host names for HTTP endpoints. Using custom domain names therefore involves dealing with TLS certificates, DNS records, and ensuring that your application can respond to requests made to these custom domains.


## Setting up DNS with Stelvio

The base class for managing DNS in Stelvio is `stelvio.dns.Dns`. You can use it to create and manage DNS records.

To use a DNS provider of your choice, Stelvio provides specific implementations for popular DNS providers:

- `stelvio.cloudflare.dns.CloudflareDns` for Cloudflare
- `stelvio.aws.route53.Route53Dns` for AWS Route 53
- more providers will be added in the future

## Configuring DNS in Stelvio

To configure DNS in your Stelvio application, you need to create an instance of the DNS provider class and pass it to your `StelvioApp` instance.
Here's an example of how to set up Cloudflare DNS in your Stelvio application:

```python
from stelvio import StelvioApp
from stelvio.cloudflare.dns import CloudflareDns
dns = CloudflareDns(
    zone_id="your-cloudflare-zone-id")

app = StelvioApp(
    "my-app",
    dns=dns,
    # other configurations...
)
```

This example initializes a Stelvio application with Cloudflare DNS. You need to replace `"your-cloudflare-zone-id"` with your actual Cloudflare zone ID.

## Managing Certificates for Domains with Stelvio

When using custom domain names, you also need to manage TLS certificates.

Stelvio provides a way to manage custom domain names with TLS certificates through the `stelvio.aws.acm.AcmValidatedDomain` class for custom domain names on AWS.

Here's an example of how to set up a custom domain with a TLS certificate in Stelvio:

```python
from stelvio.aws.acm import AcmValidatedDomain

domain = AcmValidatedDomain(
    domain_name="your-custom-domain.com"
)
```

This class will handle the creation and validation of the TLS certificate for your custom domain. You can then use this domain in your Stelvio application.
`AcmValidatedDomain` is a Stelvio component that automatically creates the following three Pulumi resources on AWS, and your DNS provider:

- `certificate`: `pulumi_aws.acm.Certificate`
- `validation_record`: `stelvio.dns.Record`
- `cert_validation`: `pulumi_aws.acm.CertificateValidation`


## Use custom domains with ApiGateway

See: [ApiGateway Custom Domains](/guides/api-gateway/#custom-domains)