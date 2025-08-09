# Changelog

## 0.4.0a7 (2025-08-10)

With this release, Stelvio supports S3 Buckets and CloudFront distributions for static websites.

### DNS & Custom domain support
- Added `stelvio.aws.cloudfront.CloudFrontDistribution` for managing CloudFront distributions
- Added `stelvio.aws.s3.S3StaticWebsite` for managing S3 static websites
- Added `stelvio.aws.s3.Bucket` for managing S3 buckets

## 0.4.0a6 (2025-08-06)

With this release, custom domains are supported for Api Gateway.

### DNS & Custom domain support
- Added `stelvio.aws.route53.Route53Dns` for managing DNS records in AWS Route 53
- Added `stelvio.cloudflare.dns.CloudflareDns` for managing DNS records in Cloudflare
- Added `stelvio.aws.acm.AcmValidatedDomain` for managing TLS certificates for custom domains in AWS
- Stelvio now automatically creates and validates TLS certificates for custom domains

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
