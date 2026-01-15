# Changelog

## 0.7.0a10 (2026-01-31)

### Bucket notifications

Stelvio supports Bucket notification events. When an object in a bucket is created, modified, or deleted, you can notify a `Queue`, invoke a Lambda function or publish to an SNS topic.

→ [Buckets Guide](guides/s3.md)

### Queues

Stelvio now supports a `Queue` component to work with SQS Queues.

→ [Queues Guide](guides/queues.md)

### SNS Topics

New `Topic` component for pub/sub messaging with Amazon SNS. Supports standard and FIFO topics, Lambda and SQS subscriptions, and filter policies for message routing.

→ [SNS Topics Guide](guides/topics.md)

### Email sending

Stelvio now offers an `Email` component to send emails using Amazon SES.

→ [Email Guide](guides/email.md)

### Function-to-Function Linking

Functions can now link to other functions, enabling Lambda-to-Lambda invocation. When you link a function to another, Stelvio automatically grants `lambda:InvokeFunction` permission and provides `function_arn` and `function_name` via the generated `Resources` object.

→ [Lambda Functions Guide](guides/lambda.md#linking-to-other-functions)

### Scheduled Tasks with Cron

New `Cron` component for running Lambda functions on a schedule using EventBridge Rules. Supports rate expressions (`rate(1 hour)`) and cron expressions (`cron(0 2 * * ? *)`), with options for custom payloads and resource linking.

→ [Cron Guide](guides/cron.md)

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

→ [Dev Mode Guide](guides/stlv-dev.md)

### S3 State Sync

Stelvio now stores infrastructure state in S3, making it ready for teams:

- **Shared state** - Multiple developers work on the same app without file syncing
- **Locking** - Concurrent deployments are blocked to prevent conflicts
- **Crash recovery** - State saves continuously; interrupted deploys resume cleanly
- **Operation history** - Track deployments across your team

State is stored in S3 bucket automatically. No configuration needed.

→ [State Management Guide](guides/state.md)

### CloudFront Router

New `Router` component for CloudFront-based routing with multiple origins - route different paths to API Gateway, Lambda Function URLs, or other backends.

→ [CloudFront Router Guide](guides/cloudfront-router.md)

### Lambda Function URLs

Direct HTTP access to Lambda functions:

```python
my_function = Function("my-func", handler="handler.main", url="public")
```

→ [Function URLs Guide](guides/lambda.md#function-urls)

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
