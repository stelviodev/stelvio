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
*   DMARC (Domain-based Message Authentication, Reporting, and Conformance) records if `dmarc` is provided.

Note that for domain identities, you must have a DNS provider configured in your Stelvio app context, or pass one explicitly to the `Email` component.
