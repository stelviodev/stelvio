"""Suppress noisy runtime warnings from Pulumi providers and gRPC.

1. Pulumi providers emit Python `warnings.warn()` for deprecations that duplicate
   the `pulumi.warn()` diagnostic we already capture and display. Filter them out
   to avoid raw stderr noise.

2. gRPC's C++ layer writes directly to fd 2 (stderr), bypassing Python's logging
   system. Redirect fd 2 to /dev/null while keeping Python's sys.stderr functional.
   See: https://github.com/grpc/grpc/issues/38703
   TODO: Remove the fd2 workaround once grpc >= 1.80.0 is available.
"""

import os
import sys
import warnings

# Pulumi providers duplicate deprecation warnings via both warnings.warn() and
# pulumi.warn(). The latter is captured by our CLI; suppress the raw Python ones.
warnings.filterwarnings("ignore", module=r"pulumi_aws\..*")
warnings.filterwarnings("ignore", module=r"pulumi_cloudflare\..*")

os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GRPC_TRACE"] = ""
os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"

_real_stderr_fd = os.dup(2)
_devnull_fd = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull_fd, 2)
os.close(_devnull_fd)
sys.stderr = os.fdopen(_real_stderr_fd, "w", buffering=1)
