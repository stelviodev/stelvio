#!/usr/bin/env bash
# Run all integration test tiers in parallel.
# Exclusive macOS host tests are skipped; this runner never passes their opt-in flag.
#
# This file is the single source of truth for default worker counts. Counts are
# chosen so tests divide evenly across workers with no straggler left running
# alone at the end. Adjust these defaults when adding or removing tests:
#   integration       — 187 tests / 11 workers (VPC-creating cases moved out)
#   integration_vpc   —  11 tests /  4 workers (account VPC quota is 10,
#                       including the default VPC)
#   integration_cf    —  14 tests /  7 workers
#   integration_dns   —  10 tests /  3 workers
#
# Usage:
#   STLV_TEST_AWS_PROFILE=<profile> ./tests/integration/run_all.sh
#   ./tests/integration/run_all.sh --cloudfront-workers 10 [pytest args...]
#
# For DNS tier, also set:
#   STLV_TEST_DNS_DOMAIN=<domain> STLV_TEST_DNS_ZONE_ID=<zone-id>

set -euo pipefail

COMMON_ARGS=(-v --tb=short)
USER_ARGS=()
INTEGRATION_DIR="tests/integration"

INTEGRATION_WORKERS=11
VPC_WORKERS=4
CLOUDFRONT_WORKERS=7
DNS_WORKERS=3

usage_error() {
    printf 'run_all.sh: %s\n' "$1" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --integration-workers)
            (($# >= 2)) || usage_error "--integration-workers requires a positive integer"
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || usage_error "--integration-workers requires a positive integer"
            INTEGRATION_WORKERS="$2"
            shift 2
            ;;
        --vpc-workers)
            (($# >= 2)) || usage_error "--vpc-workers requires a positive integer"
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || usage_error "--vpc-workers requires a positive integer"
            VPC_WORKERS="$2"
            shift 2
            ;;
        --cloudfront-workers)
            (($# >= 2)) || usage_error "--cloudfront-workers requires a positive integer"
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || usage_error "--cloudfront-workers requires a positive integer"
            CLOUDFRONT_WORKERS="$2"
            shift 2
            ;;
        --dns-workers)
            (($# >= 2)) || usage_error "--dns-workers requires a positive integer"
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || usage_error "--dns-workers requires a positive integer"
            DNS_WORKERS="$2"
            shift 2
            ;;
        *)
            USER_ARGS+=("$1")
            shift
            ;;
    esac
done

pids=()
exit_code=0

run_tier() {
    local tier="$1"
    local workers="$2"
    local -a pytest_args

    pytest_args=("$INTEGRATION_DIR" "$tier" "${COMMON_ARGS[@]}")
    if ((${#USER_ARGS[@]})); then
        pytest_args+=("${USER_ARGS[@]}")
    fi
    pytest_args+=(-n "$workers")

    uv run pytest "${pytest_args[@]}" 2>&1 &
    pids+=($!)
}

# Standard tier — 11 workers for 187 tests (no VPC creates)
run_tier --integration "$INTEGRATION_WORKERS"

# VPC tier — 4 workers for 11 tests (quota is 10 VPCs including the default VPC)
run_tier --integration-vpc "$VPC_WORKERS"

# CloudFront tier — 7 workers for 14 tests (slow teardown, mostly waiting on AWS)
run_tier --integration-cf "$CLOUDFRONT_WORKERS"

# DNS tier — only if domain env vars are set
if [[ -n "${STLV_TEST_DNS_DOMAIN:-}" && -n "${STLV_TEST_DNS_ZONE_ID:-}" ]]; then
    run_tier --integration-dns "$DNS_WORKERS"
fi

# Wait for all tiers and track failures
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        exit_code=1
    fi
done

exit $exit_code
