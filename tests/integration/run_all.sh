#!/usr/bin/env bash
# Run all integration test tiers in parallel.
#
# Worker counts are chosen so tests divide evenly across workers with no
# straggler left running alone at the end. Adjust when adding/removing tests:
#   integration    — 122 tests / 10 workers
#   integration_cf —  13 tests /  7 workers (2+2+2+2+2+2+1)
#   integration_dns—   7 tests /  4 workers (4+3)
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

# CloudFront tier — 7 workers for 13 tests (slow teardown, mostly waiting on AWS)
uv run pytest "$INTEGRATION_DIR" --integration-cf $COMMON_ARGS -n 7 &
pids+=($!)

# DNS tier — only if domain env vars are set
if [[ -n "${STLV_TEST_DNS_DOMAIN:-}" && -n "${STLV_TEST_DNS_ZONE_ID:-}" ]]; then
    uv run pytest "$INTEGRATION_DIR" --integration-dns $COMMON_ARGS -n 4 &
    pids+=($!)
fi

# Wait for all tiers and track failures
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        exit_code=1
    fi
done

exit $exit_code
