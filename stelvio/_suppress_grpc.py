"""Suppress noisy runtime warnings from Pulumi providers and gRPC.

Two separate problems:

1. Pulumi provider warnings
   Pulumi AWS/Cloudflare providers emit Python `warnings.warn()` for resource
   deprecations. But Pulumi also sends the same deprecation via `pulumi.warn()`
   which our CLI already captures and displays. Without filtering, users see
   ugly raw Python warnings duplicated on stderr.

2. gRPC/abseil stderr noise
   gRPC's C++ layer uses Google's abseil logging, which writes directly to
   file descriptor 2 (stderr), completely bypassing Python's logging system.
   This produces messages like:
     WARNING: All log messages before absl::InitializeLog() is called are written to STDERR
     I0000 00:00:... fork_posix.cc:71] Other threads are currently calling into gRPC...

   There is no Python API to suppress this. The upstream fix (a Python wrapper
   for absl::InitializeLog) is tracked in grpc/grpc#38703 but still open as of
   grpcio 1.80.0. GRPC_VERBOSITY=NONE helps with some messages but not the
   abseil init warning.

   Workaround: save the real fd 2, redirect fd 2 to /dev/null (so abseil's
   writes go nowhere), then rebuild sys.stderr from the saved fd so Python's
   own stderr still works normally.

   This can be removed once grpc/grpc#38703 is resolved and a Python-side
   suppression API ships.
"""

import os
import sys
import warnings

# --- Pulumi provider warnings ---
# Suppress raw Python deprecation warnings from providers; our CLI already
# shows the equivalent pulumi.warn() diagnostics.
warnings.filterwarnings("ignore", module=r"pulumi_aws\..*")
warnings.filterwarnings("ignore", module=r"pulumi_cloudflare\..*")

# --- gRPC/abseil stderr noise ---
# Environment variables reduce some gRPC log output but don't fully suppress
# abseil's init warning. The fd2 redirect below handles the rest.
os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GRPC_TRACE"] = ""
os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"

# Redirect fd 2 to /dev/null, rebuild sys.stderr from the saved real fd.
_real_stderr_fd = os.dup(2)
_devnull_fd = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull_fd, 2)
os.close(_devnull_fd)
sys.stderr = os.fdopen(_real_stderr_fd, "w", buffering=1)
