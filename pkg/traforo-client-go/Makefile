BINARY := traforo
VERSION := $(shell git describe --tags --exact-match 2>/dev/null || git rev-parse HEAD 2>/dev/null || echo dev)
COMMIT := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
LDFLAGS := -s -w -X main.version=$(VERSION) -X main.commit=$(COMMIT)

GO ?= go
GOFMT ?= gofmt

export CGO_ENABLED := 1
export MACOSX_DEPLOYMENT_TARGET := 15.0
export CGO_CFLAGS := $(CGO_CFLAGS) -mmacosx-version-min=15.0
export CGO_LDFLAGS := $(CGO_LDFLAGS) -mmacosx-version-min=15.0

.PHONY: build test lint vet clean race test-race format

build:
	mkdir -p build
	$(GO) build -mod=readonly -trimpath -buildvcs=false -ldflags '$(LDFLAGS)' -o build/$(BINARY) ./cmd/stelvio-traforo

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

# Retain outputs and metadata created by the developer installation script.
clean:
	rm -f build/$(BINARY)
