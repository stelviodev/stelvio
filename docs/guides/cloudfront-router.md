# AWS Cloudfront (CDN)

This guide explains how to expose different Stelvio components to the web using a Cloudfront Router.

AWS Cloudfront is primarily a CDN (Content Delivery Network). That means the Cloudfront service can globally cache resources (called origins), typically static resources like S3 buckets.

Because Cloudfront allows executing small functions to manipulate request objects before they hit the origin, we can use Cloudfront as a routing component: We can map components to paths and disable caching for dynamic origins like APIs.

## Cloudfront Router

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

    router = CloudfrontRouter("rtr-test", custom_domain=domain_name)
    router.route("/files", bucket)
    router.route("/api", api)
```

The crucial part here is the line `router = CloudfrontRouter("rtr-test", custom_domain=...)`.
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


## Why you need it

If you want to expose multiple AWS resources on the same domain, Stelvio's Cloudfront Router is the way to go.

Let's say you have an SPA (Single Page Application) with static resources and an Api Gateway as a backend. You might want to expose them on the **same domain** to avoid dealing with complex CORS (Cross-Origin Resource Sharing) settings.

## Supported Origins

As of now, Stelvio supports the following components as origins for the Cloudfront Router:

- S3 Bucket
- API Gateway
- Lambda Function (using Lambda Function URLs).


### Parameters

| Parameter       | Description                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `custom_domain` | The custom domain name for router endpoint. If provided, a DNS record will be created for the CloudFront router. Optional. A `str`. |
