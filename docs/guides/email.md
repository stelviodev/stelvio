# AWS SES (Email)

Stelvio supports creating and managing Amazon SES (Simple Email Service) identities using the `Email` component. This allows you to send emails from your applications.

!! warning
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
        "stlvEmail",
        "sender@example.com",
        dmarc=None,
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
from stlv_resources import StlvemailResource

def handler(event, context):
    client = boto3.client('sesv2')
    
    # Access the linked resource properties
    # The class name is derived from the component name (stlvEmail -> StlvemailResource)
    resources = StlvemailResource()
    SENDER = resources.email_identity_sender
    RECIPIENT = "recipient@example.com"
    
    body = "Hello from Stelvio!"

    response = client.send_email(
        FromEmailAddress=SENDER,
        Destination={
            'ToAddresses': [RECIPIENT]
        },
        Content={
            'Raw': {
                'Data': body.encode('utf-8')
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
        "stlvEmail",
        "sender@example.com",
        dmarc=None,
        sandbox=True,
    )
```

When `sandbox=True`, the linked Lambda function receives broader permissions (`"*"` resource) for sending emails, which is required when your account is in sandbox mode. Once you have [requested production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html), you can set `sandbox=False` (the default) to use more restrictive permissions.

## Event Destinations

You can configure SNS event destinations to receive notifications about email events such as bounces, complaints, and deliveries.

```python
    email = Email(
        "stlvEmail",
        "sender@example.com",
        dmarc=None,
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
