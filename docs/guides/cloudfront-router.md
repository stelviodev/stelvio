# AWS Cloudfront (CDN)

This guide explains how to expose different Stelvio components to the web using a Cloudfront Router.

AWS Cloudfront is primarily a CDN (Content Delivery Network). That means the Cloudfront service can globally cache resources (called origins), typically static resources like S3 buckets.

Because Cloudfront allows executing small functions to manipulate request objects before they hit the origin, we can use Cloudfront as a routing component: We can map components to paths and disable caching for dynamic origins like APIs.

## Router

Let's assume, our web app consists of two components:
- An **S3 Bucket** that contains publicly accessible files
- An **API Gateway** that contains our application logic

We want to access our components with the following paths:
- `/api/*` -> should hit the API Gateway
- `/files` -> should hit the S3 bucket

The way to configure this in Stelvio looks like this;

```python
@app.run
def run() -> None:
    domain_name = "example.com"

    bucket = Bucket("static-files-bucket")

    api = Api("my-api")
    api.route("GET", "/api", "functions/hello.handler")

    router = Router("rtr-test", custom_domain=domain_name)
    router.route("/files", bucket)
    router.route("/api", api)
```

The crucial part here is the line `router = Router("rtr-test", custom_domain=...)`.
This creates a Cloudfront Router component.

With this component, you can add routes to your existing components as such:
```python
router.route("/files", bucket)
router.route("/api", api)
```

You might wonder why there is another route definition from the ApiGateway:

```python
api.route("GET", "/api", "functions/hello.handler")
```

The reason for this is that the Cloudfront Router sits just in front of all other components from a visitor's perspective.
Your API Gateway handles all its internal routes by itself, as outlined in the [API Gateway Guide](/guides/api-gateway/).

The Cloudfront Route (`router.route("/api", api)`) now maps every incoming request to the API Gateway and strips the `/api` prefix. This way, your API Gateway does not need to know anything about the incoming `/api` prefix.

Similarly, the S3 Bucket has its internal structure of objects. Let's say, you have an object called "hello.txt" in your bucket. If you'd expose the bucket to the web as outlined in the [Custom Domain Guide](/guides/dns/), you'd access that file via `https://example.com/hello.txt`. However, that's not what our intention was initially: We want to access that file via `https://example.com/files/hello.txt`. This is what the Cloudfront route is for: It takes the incoming request, strips the `files/` part and directs it to the bucket origin.

#### Lambda Function URL Configuration

A standalone Lambda function (as outlined in the [Lambda Guide](/guides/lambda/)) can have a Function URL config attached.

If you want to handle a Lambda function with this router, Stelvio forces you to use the default configuration. The reason for this is that we do not allow public access to a lambda function handled by a router, but only allow accessing it through the router. The reason for this is that you want the `Router` to be the only access point to your Lambda function.

Here is how it works:

```python
# Override CORS (auth defaults to "iam")
router.route("/api", fn, function_url={"cors": {...}})

# Override streaming (auth defaults to "iam")
router.route("/api", fn, function_url={"streaming": True})
```

Key takeaway: When `auth` is omitted or set to `"default"`, `Router` defaults to `"iam"`.


#### S3 Buckets cache behavior

When routing a path to an S3 bucket, the default cache values are set like this:

```python
default_ttl = 86400 # 1 day
max_ttl: 31536000 # 1 year
```

This is a sane default to take advantage of the reduced traffic costs in Cloudfront (compared to S3).
If you need to invalidate the cache, you can use the `aws` CLI:

```bash
# Distribution ID is printed during deploy or via: stlv outputs

# Invalidate all cached content
aws cloudfront create-invalidation \
  --distribution-id E1234567890ABC \
  --paths "/*"

# Invalidate specific paths
aws cloudfront create-invalidation \
  --distribution-id E1234567890ABC \
  --paths "/index.html" "/assets/*"
```

!!! note "Costs associated with cache invalidation"
    - First 1,000 invalidation paths/month are free
    - `/*` counts as 1 path (invalidates everything)

!!! note "When to invalidate cache"
    If your S3 objects use stable (non-versioned) names (no hash, timestamp, or random suffix),
    you must invalidate the affected paths (or `/*`) after updating them; otherwise CloudFront
    may continue serving stale cached content until the TTL expires.


Future versions of Stelvio will allow customizing the cache time values.

### Forearding to external URLs

You can add an external URL (instead of a component) as a origin target for the `Router` like so:

```python
router.route("/echo", "https://example.com")
```

This means that all requests to `/echo` on the `Router`'s domain will be proxied to `https://example.com`.

**Note**: The `Host` header is rewritten, so that every request to this external URL is sent with the `Host` header of the origin domain (`example.com` in the example).


## Why you need it

If you want to expose multiple AWS resources on the same domain, Stelvio's Cloudfront Router is the way to go.

Let's say you have an SPA (Single Page Application) with static resources and an Api Gateway as a backend. You might want to expose them on the **same domain** to avoid dealing with complex CORS (Cross-Origin Resource Sharing) settings.

## Supported Origins

As of now, Stelvio supports the following components as origins for the Cloudfront Router:

- S3 Bucket
- API Gateway
- Lambda Function (using Lambda Function URLs).
- URLs

## Use with custom domains

If you're using the `custom_domain` argument for the `Router` component, keep in mind that this might conflict with existing `custom_domain` settings on the origin components.

For example, if you have set a custom domain on your API Gateway like in the following example, the same custom domain must not be used for the `Router` component:

```python
api = Api("MyApi", custom_domain='example.com')
api.route("GET", "/", "functions/api.handler")

router = Router("MyRouter", custom_domain='example.com')
router.route("/api", api)
```

It is however possible to use different sub-domains on components used by the `Router` like so:

```python
api = Api("MyApi", custom_domain='api.example.com')
api.route("GET", "/", "functions/api.handler")

router = Router("MyRouter", custom_domain='example.com')
router.route("/api", api)
```


### Parameters

| Parameter       | Description                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `custom_domain` | The custom domain name for router endpoint. If provided, a DNS record will be created for the CloudFront router. Optional. A `str`. |
