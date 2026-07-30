#!/usr/bin/env bash
# Run all integration test tiers in parallel.
#
# This file is the single source of truth for test/worker counts. Counts are
# chosen so tests divide evenly across workers with no straggler left running
# alone at the end. Adjust when adding/removing tests:
#   integration    — 175 tests / 10 workers
#   integration_cf —  14 tests /  7 workers (2+2+2+2+2+2+2)
#   integration_dns—   9 tests /  3 workers (3+3+3)
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
