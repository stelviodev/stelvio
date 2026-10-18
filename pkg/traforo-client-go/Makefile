GO ?= go
GOFMT ?= gofmt

export CGO_ENABLED := 1
export MACOSX_DEPLOYMENT_TARGET := 15.0
export CGO_CFLAGS := $(CGO_CFLAGS) -mmacosx-version-min=15.0
export CGO_LDFLAGS := $(CGO_LDFLAGS) -mmacosx-version-min=15.0

.PHONY: build test lint vet clean race test-race format

build:
	cd ../.. && uv run python pkg/traforo-client-go/scripts/build.py --build-only

test:
	$(GO) test -mod=readonly ./...

# Check formatting and run golangci-lint without rewriting source files.
lint:
	@unformatted="$$($(GOFMT) -l cmd internal)" || exit $$?; \
	if [ -n "$$unformatted" ]; then \
		printf 'Run make format to format these files:\n%s\n' "$$unformatted"; \
		exit 1; \
	fi
	golangci-lint run

vet:
	$(GO) vet -mod=readonly ./...

race:
	$(GO) test -mod=readonly -race ./...

test-race: race

format:
	$(GOFMT) -w cmd internal

# Remove local build bundles; leave the installed helper and release cache intact.
clean:
	rm -rf build/traforo-*
