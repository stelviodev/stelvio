from typing import Literal

# Type alias for supported AWS Lambda instruction set architectures
type AwsArchitecture = Literal["x86_64", "arm64"]

# Type alias for supported AWS Lambda Python runtimes
type AwsLambdaRuntime = Literal["python3.12", "python3.13"]
