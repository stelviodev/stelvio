from typing import Literal

# Type alias for supported AWS Lambda instruction set architectures
type AwsArchitecture = Literal["x86_64", "arm64"]

# Type alias for supported AWS Lambda Python runtimes
type AwsLambdaRuntime = Literal[
    "python3.10", "python3.11", "python3.12", "python3.13", "python3.14", "python3.15"
]

# Shared by Function and Layer. They live here, not in the function package, so that
# `stelvio.aws.layer` can be the first Stelvio import: the function package's __init__ loads
# its config, which imports layer.
DEFAULT_RUNTIME: AwsLambdaRuntime = "python3.12"
DEFAULT_ARCHITECTURE: AwsArchitecture = "x86_64"
