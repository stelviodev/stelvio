"""Suppress noisy gRPC/abseil C++ log messages.

gRPC's C++ layer writes directly to fd 2 (stderr), bypassing Python's logging system.
This module redirects fd 2 to /dev/null while keeping Python's sys.stderr functional.

See: https://github.com/grpc/grpc/issues/38703
TODO: Remove this workaround once grpc PR #39779 is released
"""

import os
import sys

os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GRPC_TRACE"] = ""
os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"

_real_stderr_fd = os.dup(2)
_devnull_fd = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull_fd, 2)
os.close(_devnull_fd)
sys.stderr = os.fdopen(_real_stderr_fd, "w", buffering=1)
