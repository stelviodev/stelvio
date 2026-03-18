---
template: homepage.html
title: Deploy Python to AWS in minutes, not days
hide:
  - toc
---

<div id="hero-hidden-source" style="display: none" data-video-id="L9ZdFHR9BiI">

<div class="video-wrapper" style="box-shadow: none; border: none; border-radius: 0;">
<iframe src="https://www.youtube.com/embed/L9ZdFHR9BiI?si=D7Ni9BpycT2QOstC&controls=1&modestbranding=1&rel=0" title="YouTube video player" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>
</div>

</div>

<section class="resources-section" markdown="1">
<div class="resources-container" markdown="1">
<div class="resources-header">
<h2>Supported Components</h2>
<p>Everything you need for cloud apps, in Python.</p>
</div>
<div class="resources-grid" markdown="1">
<div class="resources-nav" id="resourcesNav">
<button class="nav-btn active" onclick="openTab('function')">
<span class="nav-icon">⚡</span>
<div class="nav-text">
<h3>Function</h3>
<p>Lambda Functions</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('schedules')">
<span class="nav-icon">🕐</span>
<div class="nav-text">
<h3>Schedules</h3>
<p>Cron Expressions</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('storage')">
<span class="nav-icon">📦</span>
<div class="nav-text">
<h3>Storage</h3>
<p>S3 Buckets</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('database')">
<span class="nav-icon">💾</span>
<div class="nav-text">
<h3>Database</h3>
<p>DynamoDB</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('messaging')">
<span class="nav-icon">💬</span>
<div class="nav-text">
<h3>Messaging</h3>
<p>SQS & SNS</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('email')">
<span class="nav-icon">✉️</span>
<div class="nav-text">
<h3>Email</h3>
<p>SES</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('api')">
<span class="nav-icon">🌐</span>
<div class="nav-text">
<h3>API Gateway</h3>
<p>REST APIs</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('custom-domains')">
<span class="nav-icon">🛜</span>
<div class="nav-text">
<h3>Custom Domains</h3>
<p>Connect your resources to your own domains</p>
</div>
</button>
<button class="nav-btn" onclick="openTab('router')">
<span class="nav-icon">⛕</span>
<div class="nav-text">
<h3>Router</h3>
<p>Combine multiple resources under the same domain</p>
</div>
</button>
</div>

<div class="resources-content">

<div id="tab-function" class="tab-pane active" markdown="1">
<p>
Deploy Python functions to AWS Lambda. Link to other resources for automatic permissions.
</p>

```python
from stelvio.aws.function import Function
from stelvio.aws.s3 import Bucket

bucket = Bucket("reports")

# Link grants permissions automatically
Function(
    "processor",
    handler="functions/process.handler",
    links=[bucket],
)
```

<p>Learn more about <a href="/components/aws/lambda/" class="docs-link">Lambda functions →</a></p>

</div>

<div id="tab-schedules" class="tab-pane" markdown="1">
<p>
Schedule Lambda functions with cron expressions or rate intervals.
</p>

```python
from stelvio.aws.cron import Cron

Cron(
    "hourly-cleanup",
    "rate(1 hour)",
    "functions/cleanup.handler",
)
Cron(
    "daily-report",
    "cron(0 9 * * ? *)",
    "functions/report.handler",
)
```

<p>Learn more about <a href="/components/aws/cron/" class="docs-link">Event scheduling →</a></p>

</div>

<div id="tab-storage" class="tab-pane" markdown="1">
<p>
Securely store any amount of data with AWS S3 Buckets.
</p>

```python
from stelvio.aws.s3 import Bucket

# Create a bucket that triggers a function on new uploads
uploads = Bucket("user-uploads")
uploads.notify(
    "functions/process_upload.handler",
    events=["s3:ObjectCreated:*"],
)
```

<p>Learn more about <a href="/components/aws/s3/" class="docs-link">S3 Buckets →</a></p>
</div>

<div id="tab-database" class="tab-pane" markdown="1">
<p>
Create DynamoDB tables and link them to functions for automatic permissions.
</p>

```python
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

table = DynamoTable(
    name="users",
    partition_key="user_id",
    sort_key="created_at",
)

Function(
    "user-handler",
    handler="functions/user.handler",
    links=[table],
)
```

<p>Learn more about <a href="/components/aws/dynamo-db/" class="docs-link">DynamoDB →</a></p>

</div>

<div id="tab-messaging" class="tab-pane" markdown="1">
<p>
Decouple services with SQS queues and SNS topics.
</p>

```python
from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic

orders = Queue("orders")
orders.subscribe(
    "processor",
    "functions/process_order.handler",
)

alerts = Topic("alerts")
alerts.subscribe(
    "notifier",
    "functions/send_alert.handler",
)
```

<p>Learn more about <a href="/components/aws/queues/" class="docs-link">SQS Queues</a> & <a href="/components/aws/topics/" class="docs-link">SNS Topics →</a></p>

</div>

<div id="tab-email" class="tab-pane" markdown="1">
<p>
Send high-volume emails securely and reliably using Amazon SES.
</p>

```python
from stelvio.aws.email import Email
from stelvio.aws.function import Function

mailer = Email(
    "support-email",
    "support@example.com",
)

Function(
    "sender",
    handler="functions/send.handler",
    links=[mailer],
)
```

<p>Learn more about <a href="/components/aws/email/" class="docs-link">Email →</a></p>

</div>

<div id="tab-api" class="tab-pane" markdown="1">
<p>
Define REST APIs and route requests to Lambda functions or other resources.
</p>

```python
from stelvio.aws.apigateway import Api

api = Api(
    "payment-api",
    domain_name="api.example.com",
)

api.route("POST", "/charge", handler="functions/charge.post")
api.route("GET", "/history", handler="functions/history.get")
```

<p>Learn more about <a href="/components/aws/api-gateway/" class="docs-link">API Gateway →</a></p>

</div>

<div id="tab-custom-domains" class="tab-pane" markdown="1">
<p>
Connect your Stelvio resources to custom domains with automatic SSL certificates.
</p>

```python
app = StelvioApp(
    "my-app",
    dns=CloudflareDns("your-cloudflare-zone-id")
    # other configurations...
)

...

api = Api(
    "payment-api",
    domain_name="api.example.com",
)

api.route("POST", "/charge", handler="functions/charge.post")
api.route("GET", "/history", handler="functions/history.get")
```

<p>Learn more about <a href="/concepts/dns/" class="docs-link">Custom Domains →</a></p>

</div>


<div id="tab-router" class="tab-pane" markdown="1">
<p>
Combine multiple Stelvio resources under the same custom domain using a Router.
</p>

```python
domain_name = "example.com"

bucket = Bucket("static-files-bucket")

api = Api("my-api")
api.route("GET", "/api", "functions/hello.handler")

router = Router("router-example", custom_domain=domain_name)
router.route("/files", bucket)
router.route("/api", api)
```

<p>Learn more about <a href="/components/aws/cloudfront-router/" class="docs-link">Router →</a></p>

</div>

</div>
</section>

<section class="video-section">
    <div class="video-container">
        <div class="video-text">
            <h2>See Stelvio in Action</h2>
            <p>
                Watch how Stelvio bridges the gap between simple scripting and complex infrastructure.
                Build serverless applications with the language you love.
            </p>
        </div>
        <div class="video-player">
            <div class="video-wrapper">
                <iframe src="https://www.youtube.com/embed/W6aZFqBaH1g" title="Stelvio Dev Mode and Codespaces Demo" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
            </div>
        </div>
    </div>
</section>

<section class="features-section">
    <div class="features-header">
        <h2>Why devs love Stelvio</h2>
        <p>Ship fast without fighting infrastructure.</p>
    </div>
    <div class="features-grid">
        <div class="feature-card">
            <div class="feature-icon">🐍</div>
            <h3>Pure Python</h3>
            <p>No new language to learn. If you know Python, you know Stelvio. Your IDE, linter, and type checker all just work.</p>
        </div>
        <div class="feature-card">
            <div class="feature-icon">⚡</div>
            <h3>Smart Defaults</h3>
            <p>Sensible configurations out of the box. Simple things stay simple. Add configuration only when you need it.</p>
        </div>
        <div class="feature-card">
            <div class="feature-icon">🔗</div>
            <h3>Automated Permissions</h3>
            <p>Connect functions to databases with one line. IAM policies and environment variables are configured automatically. We call it linking.</p>
        </div>
        <div class="feature-card">
            <div class="feature-icon">🔄</div>
            <h3>Live Dev Mode</h3>
            <p><code>stlv dev</code> runs your Lambda code locally while infrastructure stays in AWS. No redeploy on every change.</p>
        </div>
        <div class="feature-card">
            <div class="feature-icon">🎛️</div>
            <h3>Full Control</h3>
            <p>Override any default when you need to. Access underlying Pulumi resources for complete customization.</p>
        </div>
        <div class="feature-card">
            <div class="feature-icon">📖</div>
            <h3>Open Source</h3>
            <p>Apache 2.0 licensed. Free forever. Contribute, fork, or self-host with confidence.</p>
        </div>
    </div>
</section>

<section class="cta-section">
    <h2>Ready to ship?</h2>
    <p>Get your first Lambda function deployed in under 5 minutes.</p>
    <a href="intro/quickstart" class="cta-button">Start Shipping →</a>
    <p class="cta-secondary-links">
        <a href="/blog/">Read the Blog</a>
    </p>
</section>
