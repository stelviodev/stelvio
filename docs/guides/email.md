# AWS SES (Email)

Stelvio supports creating and managing Amazon SES (Simple Email Service) identities using the `Email` component. This allows you to send emails from your applications.

!!! warning "Sandbox Mode"
    Your AWS account might be sandboxed and thus, only allows validated email recipients.

    You can [request production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html) for your account.

## Creating an Email Identity

You can create an email identity by instantiating the `Email` component in your `stlv_app.py`.

```python
from stelvio.aws.email import Email
from stelvio.aws.function import Function

@app.run
def run() -> None:
    # Create an email identity
    email = Email(
        "stlv_email",
        "sender@example.com",
    )

    # Link it to a function
    linked_function = Function(
        "MyFunctionA",
        handler="functions/api.handler",
        url="public",
        links=[email],
    )
```

## Sending Emails

Using the [linking mechanism](/guides/linking), you can easily access the SES identity in your Lambda functions using the regular [`boto3`](https://boto3.amazonaws.com/) library.

The `Email` component exposes the sender identity and its ARN through `stlv_resources`.

```python
import boto3
from stlv_resources import Resources

def handler(event, context):
    client = boto3.client('sesv2')
    
    # Access the linked resource properties
    resources = Resources.stlv_email
    SENDER = resources.email_identity_sender
    RECIPIENT = "recipient@example.com"
    
    body = "Hello from Stelvio!"

    response = client.send_email(
        FromEmailAddress=SENDER,
        Destination={
            'ToAddresses': [RECIPIENT]
        },
        Content={
            'Simple': {
                'Subject': {'Data': 'Test Subject'},
                'Body': {'Text': {'Data': body}}
            }
        }
    )
    
    return {"statusCode": 200, "body": "Email sent!"}
```

## Domain Identities

If you provide a domain name instead of an email address as the `sender`, Stelvio will create a domain identity.

```python
    email = Email(
        "myDomainEmail",
        "example.com",
        dmarc="v=DMARC1; p=none;",
    )
```

When using a domain identity, Stelvio automatically handles:

*   DKIM (DomainKeys Identified Mail) records
*   DMARC (Domain-based Message Authentication, Reporting, and Conformance) records

Note that for domain identities, you must have a DNS provider configured in your Stelvio app context, or pass one explicitly to the `Email` component.

### DMARC Configuration

The `dmarc` parameter is only valid for domain identities and accepts the following values:

| Value   | Behavior |
|---------|----------|
| `None`  | Uses the default DMARC policy: `"v=DMARC1; p=none;"` |
| `str`   | Uses your custom DMARC policy string |
| `False` | Explicitly disables DMARC record creation |

```python
    # Default DMARC policy
    email = Email("myEmail", "example.com")
    email = Email("myEmail", "example.com", dmarc=None)
    
    # Custom DMARC policy
    email = Email("myEmail", "example.com", dmarc="v=DMARC1; p=reject; rua=mailto:dmarc@example.com")
    
    # Disable DMARC
    email = Email("myEmail", "example.com", dmarc=False)
```

## Sandbox Mode

AWS accounts start in SES sandbox mode, which restricts sending to verified email addresses only. Stelvio provides a `sandbox` parameter to configure permissions accordingly.

```python
    email = Email(
        "stlv_email",
        "sender@example.com",
        sandbox=True,
    )
```

When `sandbox=True`, the linked Lambda function receives broader permissions (`"*"` resource) for sending emails, which is required when your account is in sandbox mode. Once you have [requested production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html), you can set `sandbox=False` (the default) to use more restrictive permissions.

## Event Destinations

You can configure SNS event destinations to receive notifications about email events such as bounces, complaints, and deliveries.

```python
    email = Email(
        "stlv_email",
        "sender@example.com",
        events=[
            {
                "name": "bounce-handler",
                "types": ["bounce", "complaint"],
                "topic_arn": "arn:aws:sns:us-east-1:123456789012:email-bounces",
            },
            {
                "name": "delivery-tracker",
                "types": ["delivery", "send"],
                "topic_arn": "arn:aws:sns:us-east-1:123456789012:email-deliveries",
            },
        ],
    )
```

### Supported Event Types

See [official AWS docs](https://docs.aws.amazon.com/ses/latest/dg/event-publishing-retrieving-sns-examples.html)

| Event Type          | Description |
|---------------------|-------------|
| `send`              | Email send initiated |
| `delivery`          | Email successfully delivered |
| `bounce`            | Email bounced |
| `complaint`         | Recipient marked email as spam |
| `reject`            | SES rejected the email |
| `open`              | Recipient opened the email |
| `click`             | Recipient clicked a link |
| `delivery-delay`    | Temporary delivery delay |
| `rendering-failure` | Template rendering failed |
| `subscription`      | Subscription preference change |

## Customization

The `Email` component supports the `customize` parameter to override underlying Pulumi resource properties. For an overview of how customization works, see the [Customization guide](customization.md).

### Resource Keys

| Resource Key          | Pulumi Args Type                                                                                                             | Description                         |
|-----------------------|------------------------------------------------------------------------------------------------------------------------------|-------------------------------------|
| `identity`            | [EmailIdentityArgs](https://www.pulumi.com/registry/packages/aws/api-docs/sesv2/emailidentity/#inputs)                       | The SES email identity              |
| `configuration_set`   | [ConfigurationSetArgs](https://www.pulumi.com/registry/packages/aws/api-docs/sesv2/configurationset/#inputs)                 | SES configuration set               |
| `verification`        | [DomainIdentityVerificationArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ses/domainidentityverification/#inputs) | Domain verification (for domains) |
| `event_destinations`  | [ConfigurationSetEventDestinationArgs](https://www.pulumi.com/registry/packages/aws/api-docs/sesv2/configurationseteventdestination/#inputs) | Event destination (when configured) |

### Example

```python
email = Email(
    "my-email",
    "notifications@example.com",
    customize={
        "identity": {
            "tags": {"Service": "notifications"},
        }
    }
)
```
