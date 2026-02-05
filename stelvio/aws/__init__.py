"""AWS components for Stelvio."""

from stelvio.aws.appsync import (
    AppSync,
    AppSyncCustomizationDict,
    AppSyncDataSource,
    AppSyncFunction,
    AppSyncResolver,
    AppSyncResources,
    CognitoAuthConfig,
    LambdaAuthConfig,
    OidcAuthConfig,
    RdsDataSourceConfig,
)

__all__ = [
    "AppSync",
    "AppSyncCustomizationDict",
    "AppSyncDataSource",
    "AppSyncFunction",
    "AppSyncResolver",
    "AppSyncResources",
    "CognitoAuthConfig",
    "LambdaAuthConfig",
    "OidcAuthConfig",
    "RdsDataSourceConfig",
]
