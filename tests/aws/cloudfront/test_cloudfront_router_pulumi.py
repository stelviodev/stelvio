import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import HttpApi, RestApi
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.function import Function
from stelvio.aws.function import function as function_module
from stelvio.aws.s3.s3 import Bucket
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..conftest import assert_urn, create_app_context_with_global_customize
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn


def test_create_resources_with_s3_bucket(pulumi_mocks):
    """Test that Router creates proper CloudFront resources for S3 bucket origin."""
    bucket = Bucket("test-bucket")
    router = Router(name="test-router")
    router.route("/static", bucket)

    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, router.resources]

    deploy()

    origin_id = f"arn:aws:s3:::{tn(TP + 'test-bucket')}"
    functions_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:function"
    not_found_arn = f"{functions_arn}/{tn(TP + 'test-router-default-404')}"
    rewrite_arn = f"{functions_arn}/{tn(TP + 'test-bucket-uri-rewrite-0')}"
    pulumi_mocks.assert_res(
        "test-router",
        R.DISTRIBUTION,
        {
            "enabled": True,
            "isIpv6Enabled": True,
            "priceClass": "PriceClass_100",
            "viewerCertificate": {"cloudfrontDefaultCertificate": True},
            "restrictions": {"geoRestriction": {"restrictionType": "none"}},
            "origins": [
                {
                    "originId": origin_id,
                    "domainName": f"{tn(TP + 'test-bucket')}.s3.{DEFAULT_REGION}.amazonaws.com",
                    "originAccessControlId": tid(TP + "test-bucket-oac-0"),
                }
            ],
            "defaultCacheBehavior": {
                "targetOriginId": origin_id,
                "allowedMethods": ["GET", "HEAD", "OPTIONS"],
                "cachedMethods": ["GET", "HEAD"],
                "compress": True,
                "viewerProtocolPolicy": "redirect-to-https",
                "forwardedValues": {
                    "queryString": False,
                    "cookies": {"forward": "none"},
                    "headers": [],
                },
                "minTtl": 0,
                "defaultTtl": 0,
                "maxTtl": 0,
                "functionAssociations": [
                    {"eventType": "viewer-request", "functionArn": not_found_arn}
                ],
            },
            "orderedCacheBehaviors": [
                {
                    "pathPattern": "/static/*",
                    "targetOriginId": origin_id,
                    "allowedMethods": ["GET", "HEAD", "OPTIONS"],
                    "cachedMethods": ["GET", "HEAD"],
                    "compress": True,
                    "viewerProtocolPolicy": "redirect-to-https",
                    "forwardedValues": {
                        "queryString": False,
                        "cookies": {"forward": "none"},
                        "headers": ["If-Modified-Since"],
                    },
                    "minTtl": 0,
                    "defaultTtl": 86400,
                    "maxTtl": 31536000,
                    "functionAssociations": [
                        {"eventType": "viewer-request", "functionArn": rewrite_arn}
                    ],
                }
            ],
        },
    )
    pulumi_mocks.assert_res(
        "test-bucket-oac-0",
        R.ORIGIN_ACCESS_CONTROL,
        {
            "description": "Origin Access Control for test-bucket route 0",
            "originAccessControlOriginType": "s3",
            "signingBehavior": "always",
            "signingProtocol": "sigv4",
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.DISTRIBUTION: 1,
            R.ORIGIN_ACCESS_CONTROL: 1,
            # URI rewrite + default 404
            R.CLOUDFRONT_FUNCTION: 2,
            R.BUCKET_POLICY: 1,
        }
    )


@pulumi.runtime.test
def test_create_resources_no_routes_raises_error(pulumi_mocks):
    """Test that creating resources with no routes raises ValueError."""
    router = Router(name="test-router")

    with raises(ValueError, match="must have at least one route"):
        router._create_resources()


def _rest_api():
    api = RestApi("api")
    api.route("GET", "/", "functions/simple.handler")
    return api


def _http_api():
    api = HttpApi("api")
    api.route("GET", "/", "functions/simple.handler")
    return api


API_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
CACHING_DISABLED_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
# No Host: API Gateway answers a foreign Host with 403
ALL_VIEWER_EXCEPT_HOST_HEADER_POLICY_ID = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
API_RESOURCES = {
    R.API_ACCOUNT: 2,
    R.LOG_GROUP: 1,
    R.ROLE: 2,
    R.ROLE_POLICY_ATTACHMENT: 1,
    R.FUNCTION: 1,
    R.LAMBDA_PERMISSION: 1,
    R.DISTRIBUTION: 1,
}


@mark.parametrize(
    ("make_origin", "default_behavior", "counts"),
    [
        param(
            lambda: Bucket("site"),
            {
                "targetOriginId": f"arn:aws:s3:::{tn(TP + 'site')}",
                "allowedMethods": ["GET", "HEAD", "OPTIONS"],
                "cachedMethods": ["GET", "HEAD"],
                "compress": True,
                "viewerProtocolPolicy": "redirect-to-https",
                "forwardedValues": {
                    "queryString": False,
                    "cookies": {"forward": "none"},
                    "headers": ["If-Modified-Since"],
                },
                "minTtl": 0,
                "defaultTtl": 86400,
                "maxTtl": 31536000,
                "functionAssociations": [],
            },
            {
                R.BUCKET: 1,
                R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
                R.ORIGIN_ACCESS_CONTROL: 1,
                R.BUCKET_POLICY: 1,
                R.DISTRIBUTION: 1,
            },
            id="bucket",
        ),
        param(
            _rest_api,
            {
                "targetOriginId": tid(TP + "api"),
                "allowedMethods": API_METHODS,
                "cachedMethods": ["GET", "HEAD"],
                "viewerProtocolPolicy": "redirect-to-https",
                "cachePolicyId": CACHING_DISABLED_POLICY_ID,
                "originRequestPolicyId": ALL_VIEWER_EXCEPT_HOST_HEADER_POLICY_ID,
                "functionAssociations": [],
            },
            API_RESOURCES
            | {
                R.REST_API: 1,
                R.API_METHOD: 1,
                R.API_INTEGRATION: 1,
                R.API_DEPLOYMENT: 1,
                R.API_STAGE: 1,
            },
            id="rest-api",
        ),
        param(
            _http_api,
            {
                "targetOriginId": tid(TP + "api"),
                "allowedMethods": API_METHODS,
                "cachedMethods": ["GET", "HEAD"],
                "viewerProtocolPolicy": "redirect-to-https",
                "cachePolicyId": CACHING_DISABLED_POLICY_ID,
                "originRequestPolicyId": ALL_VIEWER_EXCEPT_HOST_HEADER_POLICY_ID,
                "functionAssociations": [],
            },
            API_RESOURCES
            | {
                R.HTTP_API: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 1,
                R.HTTP_API_STAGE: 1,
            },
            id="http-api",
        ),
        param(
            lambda: Function("fn", handler="functions/simple.handler"),
            {
                "targetOriginId": tn(TP + "fn"),
                "allowedMethods": API_METHODS,
                "cachedMethods": ["GET", "HEAD"],
                "viewerProtocolPolicy": "redirect-to-https",
                "cachePolicyId": CACHING_DISABLED_POLICY_ID,
                "originRequestPolicyId": ALL_VIEWER_EXCEPT_HOST_HEADER_POLICY_ID,
                "functionAssociations": [],
            },
            {
                R.FUNCTION: 1,
                R.ROLE: 1,
                R.ROLE_POLICY_ATTACHMENT: 1,
                R.FUNCTION_URL: 1,
                R.ORIGIN_ACCESS_CONTROL: 1,
                R.LAMBDA_PERMISSION: 1,
                R.DISTRIBUTION: 1,
            },
            id="function",
        ),
        param(
            lambda: "https://example.com",
            {
                "targetOriginId": TP + "url-origin-0",
                "allowedMethods": API_METHODS,
                "cachedMethods": ["GET", "HEAD"],
                "compress": True,
                "viewerProtocolPolicy": "redirect-to-https",
                "forwardedValues": {
                    "queryString": True,
                    "cookies": {"forward": "all"},
                    "headers": ["*"],
                },
                "minTtl": 0,
                "defaultTtl": 0,
                "maxTtl": 0,
                "functionAssociations": [],
                # The mocks leave the Lambda@Edge `qualified_arn` unset, so no `lambdaArn`.
                "lambdaFunctionAssociations": [
                    {"eventType": "origin-request", "includeBody": True}
                ],
            },
            # The Lambda@Edge Host rewrite: function, role, policy attachment
            {R.FUNCTION: 1, R.ROLE: 1, R.ROLE_POLICY_ATTACHMENT: 1, R.DISTRIBUTION: 1},
            id="url",
        ),
    ],
)
# "" was a working catch-all before `/` was fixed; it stays one.
@mark.parametrize("path", ["/", "/*", ""])
@mark.usefixtures("mock_get_or_install_dependencies_function", "project_cwd")
def test_root_route_behavior_is_the_default_behavior(
    pulumi_mocks, path, make_origin, default_behavior, counts
):
    """The root used to get a fixed GET-only template, so a POST to an API on `/` got
    CloudFront's 403. No prefix-strip function: it would run, and bill, on every request."""
    router = Router("router")
    router.route(path, make_origin())

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    assert distribution.inputs["defaultCacheBehavior"] == default_behavior
    assert "orderedCacheBehaviors" not in distribution.inputs
    assert router.resources.cloudfront_functions == []
    pulumi_mocks.assert_res_counts(counts)


@mark.usefixtures("mock_get_or_install_dependencies_function", "project_cwd")
def test_root_route_next_to_other_routes(pulumi_mocks):
    """The root route takes the default behavior; the other routes keep theirs in order."""

    @pulumi.runtime.test
    def deploy():
        router = Router("router")
        router.route("/", Bucket("site"))
        router.route("/api", _rest_api())
        return router.resources

    deploy()

    # Routes are indexed longest first: /api 0, / 1.
    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    assert distribution.inputs["defaultCacheBehavior"] == {
        "targetOriginId": f"arn:aws:s3:::{tn(TP + 'site')}",
        "allowedMethods": ["GET", "HEAD", "OPTIONS"],
        "cachedMethods": ["GET", "HEAD"],
        "compress": True,
        "viewerProtocolPolicy": "redirect-to-https",
        "forwardedValues": {
            "queryString": False,
            "cookies": {"forward": "none"},
            "headers": ["If-Modified-Since"],
        },
        "minTtl": 0,
        "defaultTtl": 86400,
        "maxTtl": 31536000,
        "functionAssociations": [],
    }
    behaviors = distribution.inputs["orderedCacheBehaviors"]
    assert [(b["pathPattern"], b["targetOriginId"]) for b in behaviors] == [
        ("/api", tid(TP + "api")),
        ("/api/*", tid(TP + "api")),
    ]
    pulumi_mocks.assert_res(
        "api-uri-rewrite-0",
        R.CLOUDFRONT_FUNCTION,
        {"code": strip_path_pattern_function_js("/api")},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        API_RESOURCES
        | {
            R.REST_API: 1,
            R.API_METHOD: 1,
            R.API_INTEGRATION: 1,
            R.API_DEPLOYMENT: 1,
            R.API_STAGE: 1,
            R.CLOUDFRONT_FUNCTION: 1,
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.ORIGIN_ACCESS_CONTROL: 1,
            R.BUCKET_POLICY: 1,
        }
    )


def _rest_api_root_next_to_a_bucket(router):
    router.route("/", _rest_api())
    router.route("/static", Bucket("static"))


@mark.parametrize(
    ("add_routes", "scope", "expected"),
    [
        param(lambda router: router.route("/", Bucket("site")), None, "index.html", id="bucket"),
        param(
            lambda router: router.route("/*", Bucket("site")), None, "index.html", id="bucket-/*"
        ),
        param(
            lambda router: router.route("/", Bucket("site")),
            "instance",
            "home.html",
            id="bucket-instance-customize",
        ),
        param(
            lambda router: router.route("/", Bucket("site")),
            "global",
            "home.html",
            id="bucket-global-customize",
        ),
        # A Bucket elsewhere on the router doesn't count: `/` stays the API's.
        param(_rest_api_root_next_to_a_bucket, None, None, id="rest-api-root"),
    ],
)
@mark.usefixtures("mock_get_or_install_dependencies_function", "project_cwd")
def test_root_route_default_root_object(pulumi_mocks, add_routes, scope, expected):
    """`GET /` on a root bucket would ask S3 for the bucket root: 403."""
    customize = {"distribution": {"default_root_object": "home.html"}}
    if scope == "global":
        create_app_context_with_global_customize({Router: customize})

    @pulumi.runtime.test
    def deploy():
        router = Router("router", customize=customize if scope == "instance" else None)
        add_routes(router)
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    assert distribution.inputs.get("defaultRootObject") == expected


def test_slash_star_root_leaves_same_length_routes_reachable(pulumi_mocks):
    """`/x` sorts level with `/*` (same length); as an ordered behavior `/*` would sit first
    and catch `/x` too."""

    @pulumi.runtime.test
    def deploy():
        router = Router("router")
        router.route("/*", Bucket("site"))
        router.route("/x", Bucket("x"))
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    default_behavior = distribution.inputs["defaultCacheBehavior"]
    assert default_behavior["targetOriginId"] == f"arn:aws:s3:::{tn(TP + 'site')}"
    [x_behavior] = distribution.inputs["orderedCacheBehaviors"]
    assert x_behavior["pathPattern"] == "/x/*"
    assert x_behavior["targetOriginId"] == f"arn:aws:s3:::{tn(TP + 'x')}"
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 2,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 2,
            R.ORIGIN_ACCESS_CONTROL: 2,
            R.BUCKET_POLICY: 2,
            R.CLOUDFRONT_FUNCTION: 1,  # /x's prefix strip
            R.DISTRIBUTION: 1,
        }
    )


def test_customized_function_code_stays_on_root_and_wildcard_routes(pulumi_mocks):
    """Only the generated no-op rewrite is skipped on `/` and `/files/*`: a user's own
    `cloudfront_functions` code still gets a function on each route and its association."""
    code = "function handler(event) { return event.request; }"

    @pulumi.runtime.test
    def deploy():
        router = Router("router", customize={"cloudfront_functions": {"code": code}})
        router.route("/", Bucket("site"))
        router.route("/files/*", Bucket("files"))
        return router.resources

    deploy()

    pulumi_mocks.assert_res(
        "files-uri-rewrite-0", R.CLOUDFRONT_FUNCTION, {"code": code}, partial=True
    )
    pulumi_mocks.assert_res(
        "site-uri-rewrite-1", R.CLOUDFRONT_FUNCTION, {"code": code}, partial=True
    )
    functions_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:function"
    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    assert distribution.inputs["defaultCacheBehavior"]["functionAssociations"] == [
        {
            "eventType": "viewer-request",
            "functionArn": f"{functions_arn}/{tn(TP + 'site-uri-rewrite-1')}",
        }
    ]
    [files] = distribution.inputs["orderedCacheBehaviors"]
    assert files["functionAssociations"] == [
        {
            "eventType": "viewer-request",
            "functionArn": f"{functions_arn}/{tn(TP + 'files-uri-rewrite-0')}",
        }
    ]
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 2,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 2,
            R.BUCKET_POLICY: 2,
            R.ORIGIN_ACCESS_CONTROL: 2,
            R.CLOUDFRONT_FUNCTION: 2,  # the customized ones, no default 404 with a root route
            R.DISTRIBUTION: 1,
        }
    )


@mark.usefixtures("mock_get_or_install_dependencies_function", "project_cwd")
def test_wildcard_routes_pass_the_path_through(pulumi_mocks):
    """A wildcard route gets one behavior and no prefix-strip function: the origin sees the
    full path."""

    @pulumi.runtime.test
    def deploy():
        router = Router("router")
        router.route("/files/*", Bucket("files"))
        router.route("/w/*", Function("fn", handler="functions/simple.handler"))
        router.route("/u/*", "https://example.com")
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    behaviors = distribution.inputs["orderedCacheBehaviors"]
    assert [
        (b["pathPattern"], b["targetOriginId"], b["functionAssociations"]) for b in behaviors
    ] == [
        ("/files/*", f"arn:aws:s3:::{tn(TP + 'files')}", []),
        ("/w/*", tn(TP + "fn"), []),
        ("/u/*", TP + "url-origin-2", []),
    ]
    pulumi_mocks.assert_res("router-default-404", R.CLOUDFRONT_FUNCTION)
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_POLICY: 1,
            R.FUNCTION: 2,  # the routed Function + the URL route's Lambda@Edge
            R.ROLE: 2,
            R.ROLE_POLICY_ATTACHMENT: 2,
            R.FUNCTION_URL: 1,
            R.LAMBDA_PERMISSION: 1,
            R.ORIGIN_ACCESS_CONTROL: 2,
            R.CLOUDFRONT_FUNCTION: 1,  # the default 404
            R.DISTRIBUTION: 1,
        }
    )


def test_url_string_in_the_constructor_routes_becomes_a_url_origin(pulumi_mocks):
    """Same as `route(path, url)`: the constructor wraps the string before any adapter
    looks for one."""

    @pulumi.runtime.test
    def deploy():
        return Router("router", routes=[Route("/u/*", " https://example.com ")]).resources

    deploy()

    distribution = pulumi_mocks.assert_res("router", R.DISTRIBUTION)
    [origin] = distribution.inputs["origins"]
    assert (origin["originId"], origin["domainName"]) == (TP + "url-origin-0", "example.com")
    pulumi_mocks.assert_res_counts(
        {
            R.FUNCTION: 1,  # the URL route's Lambda@Edge
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.CLOUDFRONT_FUNCTION: 1,  # the default 404
            R.DISTRIBUTION: 1,
        }
    )


def test_create_resources_with_multiple_routes(pulumi_mocks):
    """Test that Router handles multiple routes correctly."""
    bucket1 = Bucket("bucket1")
    bucket2 = Bucket("bucket2")
    router = Router(name="test-router")
    router.route("/static", bucket1)
    router.route("/files", bucket2)

    @pulumi.runtime.test
    def deploy():
        return [bucket1.resources, bucket2.resources, router.resources]

    deploy()

    assert len(pulumi_mocks.created(R.DISTRIBUTION)) == 1
    # One OAC per bucket
    assert sorted(oac.name for oac in pulumi_mocks.created(R.ORIGIN_ACCESS_CONTROL)) == [
        TP + "bucket1-oac-0",
        TP + "bucket2-oac-1",
    ]


def test_create_resources_custom_domain_no_dns_raises_error():
    """Test that custom_domain without DNS provider raises DnsProviderNotConfiguredError.

    Note: This test doesn't need Pulumi mocks because it tests the error before
    resources are created.
    """
    bucket = Bucket("test-bucket")

    router = Router(name="test-router", custom_domain="example.com")
    router.route("/", bucket)

    # Should raise DnsProviderNotConfiguredError because default context has no DNS
    with raises(DnsProviderNotConfiguredError, match="DNS not configured"):
        router._create_resources()


def test_create_resources_with_custom_domain_and_dns(pulumi_mocks, app_context_with_dns):
    """Test that Router creates ACM certificate and DNS record with custom domain."""
    custom_domain = "cdn-custom-domain-dns.example.com"

    bucket = Bucket("test-bucket")
    router = Router(name="test-router-custom-domain-dns", custom_domain=custom_domain)
    router.route("/", bucket)

    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, router.resources]

    deploy()

    certificate_name = "test-router-custom-domain-dns-acm-validated-domain-certificate"
    certificate_id = tid(TP + certificate_name)
    certificate_arn = f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/{certificate_id}"
    pulumi_mocks.assert_res(
        certificate_name, R.CERTIFICATE, {"domainName": custom_domain}, partial=True
    )
    pulumi_mocks.assert_res(
        f"{certificate_name}-validation",
        R.CERTIFICATE_VALIDATION,
        {"certificateArn": certificate_arn},
        partial=True,
    )

    pulumi_mocks.assert_res(
        "test-router-custom-domain-dns-cloudfront-record",
        R.CLOUDFLARE_RECORD,
        {
            "zoneId": "test-zone-id",
            "name": custom_domain,
            "type": "CNAME",
            "content": f"{tid(TP + 'test-router-custom-domain-dns')}.cloudfront.net",
            "ttl": 1,
        },
    )

    pulumi_mocks.assert_res(
        "test-router-custom-domain-dns",
        R.DISTRIBUTION,
        {
            "aliases": [custom_domain],
            "viewerCertificate": {
                "acmCertificateArn": certificate_arn,
                "sslSupportMethod": "sni-only",
                "minimumProtocolVersion": "TLSv1.2_2021",
            },
        },
        partial=True,
    )


@pulumi.runtime.test
def test_router_parented(pulumi_mocks, app_context_with_dns):
    bucket = Bucket("test-bucket-parented")
    _ = bucket.resources

    router = Router(name="test-router-parented", custom_domain="cdn-parented.example.com")
    router.route("/", bucket)
    record = router.resources.record
    acm = router.resources.acm_validated_domain
    assert record is not None
    assert acm is not None

    def check(urns):
        record_urn, acm_urn = urns
        assert_urn(
            record_urn,
            "stelvio:aws:Router",
            R.CLOUDFLARE_RECORD,
            TP + "test-router-parented-cloudfront-record",
        )
        assert_urn(
            acm_urn,
            "stelvio:aws:Router",
            "stelvio:aws:AcmValidatedDomain",
            "test-router-parented-acm-validated-domain",
        )

    return pulumi.Output.all(record.pulumi_resource.urn, acm.urn).apply(check)


def test_route_ordering_more_specific_paths_first(pulumi_mocks):
    """Test that more specific paths come first.

    CloudFront uses the first cache behavior whose pattern matches, so /api/v1/*
    must come before /api/* or it never matches.
    """
    bucket1 = Bucket("bucket-api")
    bucket2 = Bucket("bucket-api-v1")
    router = Router(name="test-router")
    # Least specific first, so the Router has to reorder
    router.route("/api", bucket1)
    router.route("/api/v1", bucket2)

    @pulumi.runtime.test
    def deploy():
        return [bucket1.resources, bucket2.resources, router.resources]

    deploy()

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    behaviors = distribution.inputs["orderedCacheBehaviors"]
    assert [b["pathPattern"] for b in behaviors] == ["/api/v1/*", "/api/*"]


def test_route_order_preserved_in_routes_list():
    """Test that routes are stored in the order they are added."""
    bucket1 = Bucket("bucket1")
    bucket2 = Bucket("bucket2")
    bucket3 = Bucket("bucket3")

    router = Router(name="test-router")
    router.route("/a", bucket1)
    router.route("/b", bucket2)
    router.route("/c", bucket3)

    assert router.routes[0].path_pattern == "/a"
    assert router.routes[1].path_pattern == "/b"
    assert router.routes[2].path_pattern == "/c"


def test_multiple_path_patterns_create_ordered_cache_behaviors(pulumi_mocks):
    """Test that multiple path patterns result in ordered cache behaviors."""
    bucket1 = Bucket("static")
    bucket2 = Bucket("files")
    bucket3 = Bucket("uploads")
    router = Router(name="test-router")
    router.route("/static", bucket1)
    router.route("/files", bucket2)
    router.route("/uploads", bucket3)

    @pulumi.runtime.test
    def deploy():
        return [bucket1.resources, bucket2.resources, bucket3.resources, router.resources]

    deploy()

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    assert len(distribution.inputs["origins"]) == 3
    behaviors = distribution.inputs["orderedCacheBehaviors"]
    assert sorted(b["pathPattern"] for b in behaviors) == ["/files/*", "/static/*", "/uploads/*"]


def test_full_router_with_mixed_origins(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    """Test Router with multiple different origin types."""
    bucket = Bucket("static-bucket")
    fn = Function("api-function", handler="functions/simple.handler")
    router = Router(name="mixed-router")
    router.route("/static", bucket)
    router.route("/api", fn)
    router.route("/external", "https://httpbin.org/anything")

    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, fn.resources, router.resources]

    deploy()

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    assert len(distribution.inputs["origins"]) == 3
    # Function and URL routes answer their exact path too; `/static` alone would ask S3 for
    # the bucket root.
    assert [b["pathPattern"] for b in distribution.inputs["orderedCacheBehaviors"]] == [
        "/external",
        "/external/*",
        "/static/*",
        "/api",
        "/api/*",
    ]
    # The URL origin needs no OAC
    oacs = pulumi_mocks.created(R.ORIGIN_ACCESS_CONTROL)
    assert {oac.name: oac.inputs["originAccessControlOriginType"] for oac in oacs} == {
        TP + "static-bucket-oac-1": "s3",
        TP + "api-function-oac-2": "lambda",
    }


def test_router_function_and_url_routes(pulumi_mocks, project_cwd):
    """A Function route gets an IAM function URL behind an OAC, and CloudFront gets
    permission to invoke it, unless the route opts out with `auth=None`. The route's
    `function_url` config and Router's customize keys reach the Function and URL adapters."""
    customize = {
        "origin_access_controls": {"description": "custom oac"},
        "cloudfront_functions": {"comment": "custom function"},
    }

    @pulumi.runtime.test
    def deploy():
        router = Router("my-router", customize=customize)
        router.route(
            "/api",
            Function("api-fn", handler="functions/simple.handler"),
            function_url={"streaming": True},
        )
        router.route(
            "/open",
            Function("open-fn", handler="functions/simple.handler"),
            function_url={"auth": None},
        )
        router.route("/ext", "https://example.com")
        return router.resources

    deploy()

    # Routes are indexed most specific first: /open 0, /api 1, /ext 2.
    api_fn = tn(f"{TP}api-fn")
    pulumi_mocks.assert_res(
        "api-fn-router-1-url",
        R.FUNCTION_URL,
        {"functionName": api_fn, "invokeMode": "RESPONSE_STREAM", "authorizationType": "AWS_IAM"},
    )
    pulumi_mocks.assert_res(
        "open-fn-router-0-url",
        R.FUNCTION_URL,
        {
            "functionName": tn(f"{TP}open-fn"),
            "invokeMode": "BUFFERED",
            "authorizationType": "NONE",
        },
    )
    pulumi_mocks.assert_res(
        "api-fn-oac-1",
        R.ORIGIN_ACCESS_CONTROL,
        {
            "description": "custom oac",
            "originAccessControlOriginType": "lambda",
            "signingProtocol": "sigv4",
            "signingBehavior": "always",
        },
    )
    pulumi_mocks.assert_res(
        "api-fn-cloudfront-permission-1",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunctionUrl",
            "function": api_fn,
            "principal": "cloudfront.amazonaws.com",
            "functionUrlAuthType": "AWS_IAM",
            "sourceArn": f"arn:aws:cloudfront::123456789012:distribution/{tid(f'{TP}my-router')}",
        },
    )
    dist = pulumi_mocks.assert_res("my-router", R.DISTRIBUTION)
    origins = {o["originId"]: o for o in dist.inputs["origins"]}
    assert origins[api_fn]["originAccessControlId"] == tid(f"{TP}api-fn-oac-1")
    assert "originAccessControlId" not in origins[tn(f"{TP}open-fn")]
    for function in (
        "open-fn-uri-rewrite-0",
        "api-fn-uri-rewrite-1",
        "url-origin-uri-rewrite-2",
        "my-router-default-404",
    ):
        pulumi_mocks.assert_res(
            function, R.CLOUDFRONT_FUNCTION, {"comment": "custom function"}, partial=True
        )
    pulumi_mocks.assert_res_counts(
        {
            R.FUNCTION: 3,  # two routed Functions + the URL origin's Lambda@Edge
            R.ROLE: 3,
            R.ROLE_POLICY_ATTACHMENT: 3,
            R.FUNCTION_URL: 2,
            R.ORIGIN_ACCESS_CONTROL: 1,
            R.LAMBDA_PERMISSION: 1,
            R.CLOUDFRONT_FUNCTION: 4,
            R.DISTRIBUTION: 1,
        }
    )


def test_router_function_url_aliases_its_old_double_prefixed_name(
    pulumi_mocks, project_cwd, monkeypatch
):
    # The mocks never see aliases, so the seam is the FunctionUrl constructor.
    aliases: dict[str, list[tuple]] = {}
    original = function_module.FunctionUrl

    def spy(name, *args, opts, **kwargs):
        aliases[name] = [(a.name, a.parent) for a in opts.aliases if isinstance(a, pulumi.Alias)]
        return original(name, *args, opts=opts, **kwargs)

    monkeypatch.setattr(function_module, "FunctionUrl", spy)

    @pulumi.runtime.test
    def deploy():
        router = Router("my-router")
        router.route("/api", Function("api-fn", handler="functions/simple.handler"))
        return router.resources

    deploy()

    old = f"{TP}{TP}api-fn-router-0-url"
    assert aliases[f"{TP}api-fn-router-0-url"] == [
        (..., pulumi.ROOT_STACK_RESOURCE),
        (old, ...),  # under the Router
        (old, pulumi.ROOT_STACK_RESOURCE),  # stacks last deployed before parenting
    ]


def test_router_rejects_a_function_url_config_of_the_wrong_type(pulumi_mocks, project_cwd):
    router = Router("my-router")
    router.route(
        "/api", Function("api-fn", handler="functions/simple.handler"), function_url="bad"
    )

    with raises(
        TypeError, match=r"^Invalid config type: expected FunctionUrlConfig or dict, got str$"
    ):
        _ = router.resources


def test_uri_rewrite_function_comment_fits_the_cloudfront_limit(pulumi_mocks):
    """AWS rejects a CloudFront Function comment over 128 chars."""
    bucket = Bucket("b")
    router = Router("r")
    router.route("/" + "a" * 110, bucket)

    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, router.resources]

    deploy()

    [fn] = [f for f in pulumi_mocks.created(R.CLOUDFRONT_FUNCTION) if "uri-rewrite" in f.name]
    assert len(fn.inputs["comment"]) == 128
