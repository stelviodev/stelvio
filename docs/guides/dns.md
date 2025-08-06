# Working With DNS in Stelvio

When you create resources with cloud providers (such as an Api Gateway), these resources needs to be accessible via HTTP.
For example, by default, you get a URL like `https://<api-id>.execute-api.<region>.amazonaws.com/` for your Api Gateway.

In real world scenarios, you would want to use a custom domain name like `api.example.com` instead of the default one provided by the cloud provider.

There is a lot of setup and configuration needed to map a custom domain name to your cloud resources: Besides the domain name itself, you need to manage DNS records, TLS certificates, and ensure that your application can respond to requests made to these custom domains.

Stelvio offers built-in support for managing DNS records and TLS certificates autoamtically.

## Setting up DNS with Stelvio

Stelvio supports popular DNS providers:

- `stelvio.cloudflare.dns.CloudflareDns` for Cloudflare
- `stelvio.aws.route53.Route53Dns` for AWS Route 53
- more providers will be added in the future

All of these classes inherit from `stelvio.dns.Dns` and implement the necessary methods to create and manage DNS records. When creating a record, using the `create_record` method, a `stelvio.dns.Record` object is returned, which contains the details of the created record.

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

??? note
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