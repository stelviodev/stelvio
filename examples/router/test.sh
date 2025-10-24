#!/usr/bin/env bash
set -euo pipefail

echo "Running Cloudfront Router example tests..."

test_url () {
    local name="$1"
    local url="$2"
    local expected_status="$3"

    echo ""
    echo "Testing $name at $url..."

    status_code=$(curl -o /dev/null -s -w "%{http_code}\n" "$url")

    if [ "$status_code" -ne "$expected_status" ]; then
        echo "Test failed for $name: expected status $expected_status, got $status_code"
        exit 1
    fi

    echo "Test passed for $name!"
}



# test_url "S3 bucket" "https://rtr.r53.ectlnet.com/hello.txt" "200"

test_url "S3 bucket" "https://rtr.r53.ectlnet.com/files/hello.txt" "200"

test_url "API Gateway" "https://rtr.r53.ectlnet.com/api/hello" "200"


echo "All tests passed!"