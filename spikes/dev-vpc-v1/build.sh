#!/bin/sh
# Compile only; never installs, elevates, or changes host networking.
set -eu
cd "$(dirname "$0")"
if [ "$(uname -s)" != Darwin ]; then
    echo 'This P0 candidate builds on macOS only.' >&2
    exit 1
fi
mkdir -p build
/usr/bin/clang -std=c17 -Wall -Wextra -Werror -O2 native/broker/main.c -o build/broker
cd native
GOTOOLCHAIN=local go build -mod=readonly -trimpath -o ../build/forwarder .
cd ..
/usr/bin/shasum -a 256 build/broker build/forwarder > build/SHA256SUMS
/usr/bin/otool -L build/broker build/forwarder > build/runtime-libraries.txt
build/broker --version
cat build/SHA256SUMS
