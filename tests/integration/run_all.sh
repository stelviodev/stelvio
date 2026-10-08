#!/usr/bin/env bash
# Run all integration test tiers in parallel.
#
# This file is the single source of truth for test/worker counts. Counts are
# chosen so tests divide evenly across workers with no straggler left running
# alone at the end. Adjust when adding/removing tests:
#   integration       — 183 tests / 12 workers  (VPC-creating cases moved out)
#   integration_vpc   —   7 tests /  4 workers  (account VPC quota is 5,
#                       including the default VPC)
#   integration_cf    —  14 tests /  7 workers
#   integration_dns   —  10 tests /  3 workers
#   integration_tunnel — explicit STLV_TEST_TUNNEL=1, -n 0 after other lanes finish
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

# Standard tier — 12 workers for 183 tests (no VPC creates)
uv run pytest "$INTEGRATION_DIR" --integration $COMMON_ARGS -n 12 &
pids+=($!)

# VPC tier — 4 workers for 7 tests (quota is 5 VPCs including the default VPC)
uv run pytest "$INTEGRATION_DIR" --integration-vpc $COMMON_ARGS -n 4 &
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

# Exclusive controlled macOS lane: four cases, serial after every other tier.
# Requires the approved helper baseline; it owns private zones and needs no DNS tier flags.
if [[ "${STLV_TEST_TUNNEL:-0}" == "1" ]]; then
    uv run pytest "$INTEGRATION_DIR" --integration-tunnel $COMMON_ARGS -n 0 || exit_code=1
fi

exit $exit_code
