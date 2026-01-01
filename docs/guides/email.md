# Email

Send emails using Amazon Simple Email Service (SES).

The `Email` component creates an SES email identity and configuration set for sending emails. You can send from either a verified email address or a domain.

:::tip
New AWS SES accounts are in _sandbox mode_ and can only send to verified email addresses. [Request production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html) to remove these restrictions.
:::

## Creating an Email

### Using an email address

Send emails from a verified email address. When you deploy, AWS will send a verification email that you need to confirm.

```python
from stelvio.aws.email import Email

email = Email("my-email", "user@example.com")
```

### Using a domain

Send emails from any address in your domain (e.g., `support@example.com`, `hello@example.com`). The domain needs to be verified via DNS records.

```python
Email("my-email", "example.com")
```

:::note
When using a domain, you'll need to add DKIM DNS records to verify ownership. Stelvio will wait for verification during deployment.
:::

## Configuration options

### DMARC policy

Configure DMARC for domain senders to improve email deliverability:

```python
Email("my-email",
    "example.com",
    dmarc="v=DMARC1; p=quarantine; adkim=s; aspf=s;"
)
```

The default DMARC policy is `v=DMARC1; p=none;` when using a domain sender.

### Event notifications

Track email delivery events by sending notifications to SNS topics or EventBridge:

```python
from stelvio.aws.email import Email, Event

# Send bounce and complaint events to SNS
email = Email("my-email",
    "user@example.com",
    events=[
        Event(
            name="bounces",
            types=["bounce", "complaint"],
            topic="arn:aws:sns:us-east-1:123456789012:bounces"
        )
    ]
)
```

Send events to EventBridge:

```python
Email("my-email",
    "user@example.com",
    events=[
        Event(
            name="deliveries",
            types=["delivery", "send"],
            bus="arn:aws:events:us-east-1:123456789012:event-bus/default"
        )
    ]
)
```

Available event types:
- `send` - Email was sent
- `reject` - Email was rejected
- `bounce` - Email bounced
- `complaint` - Recipient marked as spam
- `delivery` - Email was delivered
- `delivery-delay` - Delivery was delayed
- `rendering-failure` - Template rendering failed
- `subscription` - Subscription event
- `open` - Email was opened
- `click` - Link in email was clicked

## Linking to Lambda functions

Link your Email component to Lambda functions to send emails:

```python
from stelvio.aws.email import Email
from stelvio.aws.function import Function

email = Email("my-email", "user@example.com")

Function("email-sender",
    handler="functions/sender.handler",
    links=[email]
)
```

Then in your Lambda function, use the AWS SDK to send emails:

```python
# functions/sender.py
import json
import boto3
from stelvio_resources import resources

ses = boto3.client("sesv2")

def handler(event, context):
    ses.send_email(
        FromEmailAddress=resources.my_email.sender,
        Destination={
            "ToAddresses": ["recipient@example.com"]
        },
        Content={
            "Simple": {
                "Subject": {"Data": "Hello from Stelvio!"},
                "Body": {"Text": {"Data": "This email was sent using Stelvio."}}
            }
        },
        ConfigurationSetName=resources.my_email.config_set
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"message": "Email sent!"})
    }
```

## Using EmailConfig

You can also use an `EmailConfig` object for configuration:

```python
from stelvio.aws.email import Email, EmailConfig, Event

config = EmailConfig(
    sender="example.com",
    dmarc="v=DMARC1; p=quarantine;",
    events=[
        Event(
            name="bounces",
            types=["bounce"],
            topic="arn:aws:sns:us-east-1:123456789012:bounces"
        )
    ]
)

email = Email("my-email", config=config)
```

## AWS resources created

For each `Email` component, Stelvio creates:

- **SES Email Identity** (`sesv2.EmailIdentity`): The verified sender (email address or domain)
- **SES Configuration Set** (`sesv2.ConfigurationSet`): For tracking and event notifications
- **Event Destinations** (`sesv2.ConfigurationSetEventDestination`): One per event configuration
- **Domain Verification** (`ses.DomainIdentityVerification`): Only for domain senders, waits for DNS verification

## Properties

The Email component exposes these properties:

| Property | Description |
|----------|-------------|
| `sender` | The email address or domain used for sending |
| `config_set_name` | The name of the SES configuration set |
| `identity_arn` | The ARN of the SES email identity |

## Linked properties

When linked to a Lambda function, these properties are available:

| Property | Description |
|----------|-------------|
| `sender` | The sender email address or domain |
| `config_set` | The configuration set name |
