#!/usr/bin/env bash
# Run all integration test tiers in parallel.
#
# This file is the single source of truth for test/worker counts. Worker counts
# limit concurrent AWS deployments; pytest-xdist schedules tests dynamically.
# Test durations vary, so even division does not guarantee an even finish.
# Adjust test counts when adding/removing tests:
#   integration     — 188 tests / 10 workers
#   integration_cf  —  14 tests /  7 workers
#   integration_docdb—   4 tests /  2 workers
#   integration_dns —  10 tests /  3 workers
#
# Usage:
#   STLV_TEST_AWS_PROFILE=<profile> ./tests/integration/run_all.sh
#
# For DNS tier, also set:
#   STLV_TEST_DNS_DOMAIN=<domain> STLV_TEST_DNS_ZONE_ID=<zone-id>

set -euo pipefail

COMMON_ARGS="-v --tb=short"
INTEGRATION_DIR="tests/integration"

pids=()
exit_code=0

# Standard tier — 10 workers
uv run pytest "$INTEGRATION_DIR" --integration $COMMON_ARGS -n 10 &
pids+=($!)

# CloudFront tier — 7 workers for 14 tests (slow teardown, mostly waiting on AWS)
uv run pytest "$INTEGRATION_DIR" --integration-cf $COMMON_ARGS -n 7 &
pids+=($!)

# DocumentDB tier — 2 workers for 4 long-running cluster tests
uv run pytest "$INTEGRATION_DIR" --integration-docdb $COMMON_ARGS -n 2 &
pids+=($!)

# DNS tier — only if domain env vars are set
if [[ -n "${STLV_TEST_DNS_DOMAIN:-}" && -n "${STLV_TEST_DNS_ZONE_ID:-}" ]]; then
    uv run pytest "$INTEGRATION_DIR" --integration-dns $COMMON_ARGS -n 3 &
    pids+=($!)
fi

# Wait for all tiers and track failures
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        exit_code=1
    fi
done

exit $exit_code
