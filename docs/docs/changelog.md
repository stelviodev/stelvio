# Changelog

## 0.10.0b6 (2026-MM-DD)

### Bug Fixes

- **DNS record parenting.** DNS records created by Stelvio components (ACM validation, `ApiDomain` / `RestApi` custom-domain aliases, Email DKIM/DMARC, CloudFront/Router aliases, Cognito custom domain, AppSync custom domain) are parented under their owning component when using built-in Route 53 or Cloudflare providers. Operators may see Pulumi URN parent changes for those records (a graph nesting fix, not a DNS payload change) — Stelvio’s root-stack aliases migrate existing resources without delete/recreate where possible.
- **ACM nested under custom-domain owners.** `AcmValidatedDomain` created for custom domains on `RestApi`, `AppSync`, Cognito `UserPool`, `CloudFrontDistribution`, and `Router` is now parented under the owning component (matching `ApiDomain`). Operators may see Pulumi URN parent changes for those ACM resources; root-stack aliases migrate existing resources without delete/recreate where possible.
- **Subscription parenting.** `TopicSubscription`, `TopicQueueSubscription`, `QueueSubscription`, `DynamoSubscription`, and `BucketNotifySubscription` are now parented under their owning `Topic`/`Queue`/`DynamoTable`/`Bucket`, so they nest in the resource tree and deploy output instead of sitting at the stack root. Root-stack aliases migrate existing stacks in place — no replacements. Subscription constructors also accept a keyword-only `parent`, matching `Function`.

### Breaking Changes
- **Custom `Dns` adapters must accept `opts`.** `create_record` and `create_caa_record` now require keyword-only `opts: ResourceOptions | None = None` on the public `Dns` protocol. Stelvio always passes `opts=` when creating records. Adapters that omit the parameter raise `TypeError`. There is no compatibility shim or deprecation window.

  Before:

  ```python
  def create_record(self, resource_name, name, record_type, value, ttl=1):
      ...
  ```

  After:

  ```python
  def create_record(
      self, resource_name, name, record_type, value, ttl=1, *, opts=None
  ):
      ...  # forward opts to the Pulumi record resource when possible
  ```

  Apps using only built-in Route 53 or Cloudflare need no adapter changes.

- `Api` component (AWS API Gateway v1) is renamed to `RestApi`. **Caution**: This change will affect existing deployments. Users with a custom domain should expect the update to fail on the duplicate domain: remove the custom domain first (by setting `custom_domain=None` and redeploy), then re-add it and deploy again. For users without a custom domain, the update should succeed without issues, but the `invoke_url` will change on redeploy. This behavior is expected as Stelvio will remove existing API and recreate it.

→ [REST API Guide](components/aws/rest-api.md)

### API Gateway v2 (HTTP API) Support
- Stelvio now supports AWS API Gateway v2 (HTTP API) with the new `HttpApi` component. It provides a simpler, faster, and cheaper alternative to the existing `RestApi` component.

→ [HTTP API Guide](components/aws/http-api.md)

### API Gateway v2 (WebSocket API) Support

New `WebsocketApi` component for API Gateway v2 WebSocket APIs: `$connect`/`$disconnect`/`$default` and custom action routes on Lambda, Lambda or IAM auth on `$connect`, custom domains, and linking for the management API.

→ [WebSocket API Guide](components/aws/websocket-api.md)

`ApiDomain` now accepts `certificate_arn` so HttpApi and WebsocketApi users can attach an existing ACM certificate (for example a wildcard) instead of letting Stelvio issue one.

### Dev Mode Enhancements

Improved error handling and debugging in `stlv dev`: no longer silently returns None when the handler file is missing or the handler function name doesn't exist; sanitizing underscores as first character in app names.

### VPC

New `Vpc` component for Amazon VPC networking. Creates a /16 VPC with an internet gateway and three subnet tiers — public, private, and isolated — each with one subnet and its own route table per availability zone. Optional managed NAT gateways give private subnets internet access — one per AZ (default) or a single shared gateway; optionally bring your own Elastic IPs via allocation IDs. Choose availability zones by count or by name.

→ [VPC Guide](components/aws/vpc.md)

### Component Customization

- Customization values can now be **callables** that receive the resource's props and return modified props, enabling dynamic customization based on component properties. Callables give full control—they receive component-level values and can decide how to merge them.
- **Global `customize` dicts now act as defaults, not overrides.** Component-level values take precedence over global dict defaults. For example, with a global `memory=512` for all functions, `Function("my-fn", handler="...", memory=1024)` deploys with `1024`. (This precedence rule applies to dicts; callable customizers receive all values and decide their own precedence.)

→ [Customization Guide](concepts/customization.md)


## 0.9.0b5 (2026-04-13)

### Cognito User Pools & Identity Pools

New `UserPool` and `IdentityPool` components for user authentication with Amazon Cognito. Supports email/phone sign-in, app clients, social login providers, Lambda triggers, MFA, password policies, and SES email integration — with automatic IAM permission wiring via links. `IdentityPool` provides federated identities with authenticated and unauthenticated role management.

→ [Cognito Guide](components/aws/cognito.md)

### CLI

- Redesign CLI output: component-grouped display with nested trees, property diffs, and data-loss replacement warnings for `diff`, `deploy`, `refresh`, and `destroy`.
- Add `--json` summaries for all commands and `--stream` NDJSON output for `deploy` and `destroy`.
- Redesign `stlv outputs` to show component URLs and user-defined exports separately.
- Add `export_output` helper (`from stelvio import export_output`) for user-defined stack exports.
- Add `--outputs` flag to `stlv state list` for debugging raw Pulumi outputs per resource.
- Add structured CLI exit codes. Require explicit environment in CI.

→ [Using Stelvio CLI](intro/using-cli.md)

### Breaking Changes

- **Automatic stack exports removed.** Components no longer call `pulumi.export()` automatically (e.g., `function_api_arn`, `queue_orders_url`). If you read stack outputs in scripts or CI, use `export_output()` in your `stlv_app.py` to explicitly export the values you need. Component URLs (Api, AppSync, etc.) are still shown in `stlv outputs` via `register_outputs`.

### Python 3.14 Support

Stelvio now supports Python 3.14.

### Default App Config

`@app.config` is now optional. If omitted, Stelvio uses `StelvioAppConfig()` with default values. Add `@app.config` only when you need to customize AWS settings, environments, tags, DNS, or component customizations.

→ [StelvioApp Guide](concepts/stelvio-app.md)

## 0.8.0b4 (2026-03-14)

### AppSync 

Stelvio now offers an `AppSync` component to manage GraphQL APIs with AWS AppSync.

→ [AppSync Guide](components/aws/appsync.md)

### Tagging

Stelvio now supports tagging AWS resources at two levels:

- Global tags in `StelvioAppConfig` apply to all AWS resources through provider default tags
- Per-component tags let you override or extend tags for specific components

Precedence: component \> global \> auto-tags (`stelvio:app`, `stelvio:env`)

→ [Tagging Guide](concepts/tags.md)

### Internals

- Components are now Pulumi `ComponentResource` nodes with proper parent-child ownership in the resource tree

### Breaking Changes

- `customize` is now a keyword-only argument on all component constructors


## 0.7.2b3 (2026-02-28)

This is a bug-fix release.

### Bug Fixes

- Fix Route53 DNS adapter returning incorrect record value, which broke DNS validation when using Route53 as DNS provider
- Fix FIFO queue subscribe creating invalid Lambda function names when queue name contains dots

### Breaking Changes

- **Email component output keys renamed**: Output keys now follow the same `{type}_{name}_{field}` underscore convention as all other components. If you read Pulumi outputs from the Email component, update your references (e.g. `notifications-ses-identity-arn` → `email_notifications_ses_identity_arn`). Note: auto-exported component outputs were later removed entirely in 0.8.0b5; use `export_output` for custom values.

## 0.7.1b2 (2026-02-20)

This is a bug-fix release.

### DNS & Custom domain support

- Fix a bug where `stelvio.aws.acm.AcmValidatedDomain` was not properly creating the validation record in `us-east-1` region, which is required for Cloudfront distributions.

## 0.7.0b1 (2026-01-31)

### Queues

Stelvio now supports a `Queue` component to work with SQS Queues.

→ [Queues Guide](components/aws/queues.md)

### SNS Topics

New `Topic` component for pub/sub messaging with Amazon SNS. Supports standard and FIFO topics, Lambda and SQS subscriptions, and filter policies for message routing.

→ [SNS Topics Guide](components/aws/topics.md)

### Email Sending

Stelvio now offers an `Email` component to send emails using Amazon SES.

→ [Email Guide](components/aws/email.md)

### Scheduled Tasks with Cron

New `Cron` component for running Lambda functions on a schedule using EventBridge Rules. Supports rate expressions (`rate(1 hour)`) and cron expressions (`cron(0 2 * * ? *)`), with options for custom payloads and resource linking.

→ [Cron Guide](components/aws/cron.md)

### Function-to-Function Linking

Functions can now link to other functions, enabling Lambda-to-Lambda invocation. When you link a function to another, Stelvio automatically grants `lambda:InvokeFunction` permission and provides `function_arn` and `function_name` via the generated `Resources` object.

→ [Lambda Functions Guide](components/aws/lambda.md#linking-to-other-functions)

### Bucket Notifications

Stelvio supports Bucket notification events. When an object in a bucket is created, modified, or deleted, you can notify a `Queue`, invoke a Lambda function or publish to an SNS topic.

→ [Buckets Guide](components/aws/s3.md)

### Pulumi Resource Customization

This version allows overriding any underlying Pulumi resource property using the `customize` parameter, e.g.:

```python
bucket = Bucket("my-bucket", customize={"bucket": {"force_destroy": True}})
```

→ [Customization Guide](concepts/customization.md)

### Full Payload Support in Dev Mode

Dev mode now supports the same payload limits as Lambda: 6 MB (sync) and 1 MB (async).

## 0.6.1a9 (2025-12-30)

This is a bug-fix release.

- Fix import handling for locally executed Lambda functions (dev mode)
- Fix environment variables for locally executed Lambda functions (dev mode)

## 0.6.0a8 (2025-12-25)

We've been busy this holiday season! Here's our Christmas release 🎄

### Dev Mode (`stlv dev`) 🚀

Run your Lambda code locally while everything else stays in AWS:

```bash
stlv dev
```

Edit your function, hit refresh, see the result. No re-deploy, no waiting.

- Instant code changes - just save and refresh
- `print()` and exceptions appear right in your terminal
- Attach your favorite debugger
- Same API Gateway URL, same Function URLs - everything just works

→ [Dev Mode Guide](concepts/dev-mode.md)

### S3 State Sync

Stelvio now stores infrastructure state in S3, making it ready for teams:

- **Shared state** - Multiple developers work on the same app without file syncing
- **Locking** - Concurrent deployments are blocked to prevent conflicts
- **Crash recovery** - State saves continuously; interrupted deploys resume cleanly
- **Operation history** - Track deployments across your team

State is stored in S3 bucket automatically. No configuration needed.

→ [State Management Guide](concepts/state.md)

### CloudFront Router

New `Router` component for CloudFront-based routing with multiple origins - route different paths to API Gateway, Lambda Function URLs, or other backends.

→ [CloudFront Router Guide](components/aws/cloudfront-router.md)

### Lambda Function URLs

Direct HTTP access to Lambda functions:

```python
my_function = Function("my-func", handler="handler.main", url="public")
```

→ [Function URLs Guide](components/aws/lambda.md#function-urls)

### Other Improvements

- **Cognito scopes** - OAuth scope validation on API Gateway routes
- **Simplified DynamoDB subscriptions** - Cleaner `subscribe()` API
- **AWS profile/region** - Properly respects system settings

### Notes

Auto-generated routing for multiple handlers in the same file has been removed. Routes now create separate Lambda functions. To share a Lambda, use an explicit `Function` instance.

## 0.5.0a7 (2025-10-31)

With this release, Stelvio gets:

- a S3StaticWebsite component for S3 static website hosting with CloudFront CDN and optional custom domain support 
- support for DynamoDB streams and subscriptions.
- support for Authorizers and CORS for `Api`

### Static Website Hosting with S3 and CloudFront
- Added `stelvio.aws.s3.S3StaticWebsite` for managing S3 buckets for static website hosting with CloudFront CDN and optional custom domain support

### DynamoDB Streams
- Added `stream` property and `subscribe` method to the `DynamoTable` component so you can easily enable streams and add lambda that listens to the changes in the table.

### Api gateway authorizers
- Added `add_token_authorizer`, `add_request_authorizer` and `add_cognito_authorizer` so you can add different authorizers.
- Added `default_auth` property to set default authorizers for all endpoints and methods
- Added `auth` param to the `route` method to set authorizer on per route basis.

### Api gateway CORS

- Added `CorsConfig` and `CorsConfigDict` classes that can be used to pass to the new `cors` param of `Api` and its config classes(`ApiConfig` and `ApiConfigDict`) to configure cors settings of your Api gateway. 

## 0.4.0a6 (2025-09-05)

With this release, S3 buckets, custom domains (including Cloudflare) for ApiGateway and DynamoDB Indexes are supported.

### DNS & Custom domain support
- Added `stelvio.aws.route53.Route53Dns` for managing DNS records in AWS Route 53
- Added `stelvio.cloudflare.dns.CloudflareDns` for managing DNS records in Cloudflare
- Added `stelvio.aws.acm.AcmValidatedDomain` for managing TLS certificates for custom domains in AWS
- Stelvio now automatically creates and validates TLS certificates for custom domains

### S3 Bucket Support
- Added `stelvio.aws.s3.Bucket` for managing S3 buckets

### DynamoDb Indexes Support
- Added support for DynamoDB local and global indexes.

### Internal improvements & Fixes
- better docs
- `DynamoTableConfig`
- fix so now we can have same routes in different API Gateways
- fix to make sure generated roles and policy names with within AWS limits
- fixed flaky tests
- properly handling  API Gateway account and role and correctly displaying in CLI 

## 0.3.0a5 (2025-07-14)

### 🎉 Major Release: Complete CLI Experience

This release transforms Stelvio from a library into a complete development 
platform with a dedicated CLI.

#### Stelvio CLI (`stlv` command)

- **`stlv init`** - Initialize new projects with interactive AWS setup
- **`stlv deploy`** - Deploy with real-time progress display
- **`stlv diff`** - Preview changes before deploying  
- **`stlv destroy`** - Clean up resources safely
- **`stlv refresh`** - Sync state with actual AWS resources
- **`stlv version`** - Check your Stelvio version

#### Automatic Pulumi Management

- Zero-setup deployment - Pulumi installed automatically
- No more manual Pulumi configuration or project setup

#### Environments

- Personal environments (defaults to your username)
- Shared environments for team collaboration
- Environment-specific resource naming and isolation

#### Automatic Passphrase Management

- Generates and stores passphrases in AWS Parameter Store
- No more manual passphrase handling

#### Rich Console Output 🎨

- Color-coded operations (green=create, yellow=update, red=delete)
- Real-time deployment progress with operation timing
- Resource grouping and operation summaries
- Optional `--show-unchanged` flag for detailed views


#### New StelvioApp Architecture

- Clean decorator-based configuration with `@app.config` and `@app.run`

#### Consistent Resource Naming

- All resources get `{app}-{env}-{name}` naming pattern
- Prevents resource collisions across different deployments

#### Enhanced API Gateway Support

- Fixed multiple environment deployment issues
- Handles existing CloudWatch roles correctly

#### 🐛 Bug Fixes & Improvements

- Better error messages and debugging information
- Improved logging system
- Enhanced confirmation prompts for destructive operations

## 0.2.0a4 (2025-05-14)

- Lambda Function dependencies
- Lambda Layers
- More tests for faster future progress

## 0.1.0a2 (2025-02-14)

- Maintenance release
- Fixed bug when route couldn't be created if it had just default config
- Added better checks so Stelvio informs you if there's route conflicts
- Added tests



## 0.1.0a1 (2025-01-31)

- Initial release
- Very basic support for:

    - AWS Lambda
    - Dynamo DB Table
    - API Gateway
