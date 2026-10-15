#!/usr/bin/env bash
# Run all integration test tiers in parallel.
#
# This file is the single source of truth for test/worker counts. Counts are
# chosen so tests divide evenly across workers with no straggler left running
# alone at the end. Adjust when adding/removing tests:
#   integration       — 185 tests / 12 workers  (VPC-creating cases moved out)
#   integration_vpc   —   8 tests / up to 4 workers (remaining regional VPC capacity)
#   integration_cf    —  14 tests /  7 workers
#   integration_dns   —  10 tests /  3 workers
#
# Usage:
#   STLV_TEST_AWS_PROFILE=<profile> ./tests/integration/run_all.sh
#
# For DNS tier, also set:
#   STLV_TEST_DNS_DOMAIN=<domain> STLV_TEST_DNS_ZONE_ID=<zone-id>

set -euo pipefail

COMMON_ARGS=(-v --tb=short)
USER_ARGS=("$@")
INTEGRATION_DIR="tests/integration"

# Fail before starting any tier if existing VPCs leave no room for another test.
vpc_workers="$(uv run python -m tests.integration.vpc_capacity)"

pids=()
exit_code=0

# Standard tier — 12 workers for 185 tests (no VPC creates)
uv run pytest "$INTEGRATION_DIR" --integration "${COMMON_ARGS[@]}" "${USER_ARGS[@]}" -n 12 2>&1 &
pids+=($!)

# VPC tier — one VPC per worker, capped at 4 and remaining regional capacity.
uv run pytest "$INTEGRATION_DIR" --integration-vpc "${COMMON_ARGS[@]}" "${USER_ARGS[@]}" -n "$vpc_workers" 2>&1 &
pids+=($!)

# CloudFront tier — 7 workers for 14 tests (slow teardown, mostly waiting on AWS)
uv run pytest "$INTEGRATION_DIR" --integration-cf "${COMMON_ARGS[@]}" "${USER_ARGS[@]}" -n 7 2>&1 &
pids+=($!)

# DNS tier — only if domain env vars are set
if [[ -n "${STLV_TEST_DNS_DOMAIN:-}" && -n "${STLV_TEST_DNS_ZONE_ID:-}" ]]; then
    uv run pytest "$INTEGRATION_DIR" --integration-dns "${COMMON_ARGS[@]}" "${USER_ARGS[@]}" -n 3 2>&1 &
    pids+=($!)
fi

# Wait for all tiers and track failures
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        exit_code=1
    fi
done

exit $exit_code
