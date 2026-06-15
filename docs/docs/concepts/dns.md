# Working With DNS in Stelvio

When you create resources with cloud providers (such as an Api Gateway), these resources needs to be accessible via HTTP.
For example, by default, you get a URL like `https://<api-id>.execute-api.<region>.amazonaws.com/` for your Api Gateway.

In real world scenarios, you would want to use a custom domain name like `api.example.com` instead of the default one provided by the cloud provider.

There is a lot of setup and configuration needed to map a custom domain name to your cloud resources: Besides the domain name itself, you need to manage DNS records, TLS certificates, and ensure that your application can respond to requests made to these custom domains.

Stelvio handles all of this for you automatically with a very simple setup.

## Supported providers

Stelvio supports popular DNS providers:

- `stelvio.cloudflare.dns.CloudflareDns` for Cloudflare
- `stelvio.aws.dns.Route53Dns` for AWS Route 53
- more providers will be added in the future


## Configuring a DNS provider

Create an instance of a DNS provider and return it from `@app.config` as part of `StelvioAppConfig`:

```python
from stelvio.app import StelvioApp
from stelvio.cloudflare.dns import CloudflareDns
from stelvio.config import StelvioAppConfig

app = StelvioApp("my-app")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    dns = CloudflareDns(zone_id="your-cloudflare-zone-id")
    return StelvioAppConfig(dns=dns)
```

Replace `"your-cloudflare-zone-id"` with your actual Cloudflare zone ID.

Once configured, components that support custom domains (Api Gateway, CloudFront, Cognito, AppSync, Email and others) use this provider automatically. You don't need to wire it up per resource.

## Using a custom domain

With a DNS provider configured, you can set a custom domain on any supporting component just by passing `domain_name`. For example, with API Gateway:

```python
from stelvio.aws.api_gateway import Api

@app.run
def run() -> None:
    api = Api("my-api", domain_name="api.example.com")
```

Stelvio will create the TLS certificate, add the DNS records, and map the domain to the API Gateway endpoint. No manual DNS setup needed.

See [API Gateway Custom Domains](../components/aws/api-gateway.md#custom-domains) for more details.

## Per-component DNS override

The `Email` component accepts a `dns` parameter directly. This is useful when your email sender domain lives on a different DNS provider than your app's main domain — for example, your app uses Cloudflare, but emails are sent from a domain whose DNS is in Route 53. SES needs DKIM/SPF records on the email's domain to verify the sender identity, so Email may need a different provider than the one in `StelvioAppConfig`.

Other components don't have this split — they always use the provider from `StelvioAppConfig`.

??? note "Managing Certificates for Domains with Stelvio"

    When using custom domain names, you also need to manage TLS certificates.

    Stelvio provides a way to manage custom domain names with TLS certificates through the `stelvio.aws.acm.AcmValidatedDomain` class for custom domain names on AWS.

    Here's an example of how to set up a custom domain with a TLS certificate in Stelvio:

    ```python
    from stelvio.aws.acm import AcmValidatedDomain

    domain = AcmValidatedDomain(
        domain_name="your-custom-domain.com"
    )
    ```

    This class will handle the creation and validation of the TLS certificate for your custom domain.  
    You can then use this domain in your Stelvio application.

    **However, Stelvio usually handles this step for you (e.g. when using custom domain with API Gateway)**

    `AcmValidatedDomain` is a Stelvio component that automatically creates the following three Pulumi resources on AWS, and your DNS provider:

    - `certificate`: `pulumi_aws.acm.Certificate`
    - `validation_record`: `stelvio.dns.Record`
    - `cert_validation`: `pulumi_aws.acm.CertificateValidation`
