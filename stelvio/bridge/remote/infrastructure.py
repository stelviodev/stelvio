import time
from dataclasses import dataclass
from typing import final

import boto3


@final
@dataclass(frozen=True)
class AppSyncResource:
    """AppSync Event API resource details."""

    api_id: str
    http_endpoint: str
    realtime_endpoint: str
    api_key: str


def discover_or_create_appsync(
    region: str = "us-east-1", profile: str | None = None
) -> AppSyncResource:
    """
    Discover AppSync Event API by name, or create if doesn't exist.

    Lists all APIs, finds by name, creates if needed.
    No storage - always fresh discovery.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client("appsync")

    # print(f"Discovering AppSync Event API in {region}...")

    # Check if AppSync API exists
    return find_or_create_appsync_api(client)


def find_or_create_appsync_api(client: boto3.client) -> AppSyncResource:
    """Find existing AppSync API by name or create new one."""
    api_name = "stelvio"

    # List all APIs and find by name (same as SST)
    paginator = client.get_paginator("list_apis")
    for page in paginator.paginate():
        for api in page.get("apis", []):
            if api["name"] == api_name:
                # print(f"✓ Found existing API: {api['apiId']}")

                # Get full API details (list_apis doesn't include dns field)
                api_details = client.get_api(apiId=api["apiId"])
                full_api = api_details["api"]

                # Get API key
                keys_response = client.list_api_keys(apiId=api["apiId"])
                api_key = None
                if keys_response.get("apiKeys"):
                    api_key = keys_response["apiKeys"][0]["id"]

                return AppSyncResource(
                    api_id=full_api["apiId"],
                    http_endpoint=full_api["dns"]["HTTP"],
                    realtime_endpoint=full_api["dns"]["REALTIME"],
                    api_key=api_key,
                )

    # Not found - create it
    # print(f"AppSync API '{api_name}' not found, creating...")
    return create_appsync_api(client, api_name)


def create_appsync_api(client: boto3.client, api_name: str) -> AppSyncResource:
    """Create new AppSync Event API."""

    # Create API
    # print("  Creating Event API...")
    api_response = client.create_api(
        name=api_name,
        eventConfig={
            "authProviders": [{"authType": "API_KEY"}, {"authType": "AWS_IAM"}],
            "connectionAuthModes": [{"authType": "API_KEY"}],
            "defaultPublishAuthModes": [{"authType": "API_KEY"}],
            "defaultSubscribeAuthModes": [{"authType": "API_KEY"}],
        },
    )

    api_id = api_response["api"]["apiId"]
    http_endpoint = api_response["api"]["dns"]["HTTP"]
    realtime_endpoint = api_response["api"]["dns"]["REALTIME"]

    # print(f"  ✓ API created: {api_id}")

    # Create channel namespace
    # print("  Creating channel namespace 'stelvio'...")
    client.create_channel_namespace(
        apiId=api_id,
        name="stelvio",
        subscribeAuthModes=[{"authType": "API_KEY"}],
        publishAuthModes=[{"authType": "API_KEY"}],
    )
    # print("  ✓ Channel namespace created")

    # Create API key
    # print("  Creating API key...")
    key_response = client.create_api_key(
        apiId=api_id,
        expires=int(time.time()) + (365 * 24 * 60 * 60),  # 1 year
    )
    api_key = key_response["apiKey"]["id"]
    # print(f"  ✓ API key created")

    return AppSyncResource(
        api_id=api_id,
        http_endpoint=http_endpoint,
        realtime_endpoint=realtime_endpoint,
        api_key=api_key,
    )


# if __name__ == "__main__":
#     import sys

#     region = sys.argv[1] if len(sys.argv) > 1 else "us-east-1"
#     profile = sys.argv[2] if len(sys.argv) > 2 else None

#     # Discover or create AppSync API (runtime discovery, like SST)
#     config = discover_or_create_appsync(region, profile)

#     print("\n✓ AppSync Event API ready!")
#     print(f"  API ID: {config['api_id']}")
#     print(f"  HTTP endpoint: {config['http_endpoint']}")
#     print(f"  Realtime endpoint: {config['realtime_endpoint']}")
#     print(f"  API key: {config['api_key'][:20]}...")

#     print("\n📝 Environment variables for stub Lambda:")
#     print(f"  STLV_APPSYNC_HTTP={config['http_endpoint']}")
#     print(f"  STLV_APPSYNC_REALTIME={config['realtime_endpoint']}")
#     print(f"  STLV_APPSYNC_API_KEY={config['api_key']}")

#     print("\nNext steps:")
#     print("1. Deploy stub Lambda with above env vars")
#     print("2. Run local dev server (will discover same API)")
#     print("\n💡 No storage needed - API discovered at runtime!")
