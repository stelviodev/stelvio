GO ?= go
GOLANGCI_LINT ?= golangci-lint

.PHONY: build test race vet lint format fuzz fuzz-snapshot fuzz-dns fuzz-control

build:
	@test -f ../../pyproject.toml && test -f ../../stelvio/tunnel/assets.py || { \
		echo "make build requires the Stelvio monorepo; use its tagged release workflow from a standalone split checkout" >&2; \
		exit 2; \
	}
	cd ../.. && uv run python pkg/traforo-client-go/scripts/build.py --build-only

test:
	$(GO) test -mod=readonly ./...

race:
	$(GO) test -mod=readonly -race ./...

vet:
	$(GO) vet -mod=readonly ./...

lint:
	$(GOLANGCI_LINT) fmt --diff
	$(GOLANGCI_LINT) run

format:
	$(GOLANGCI_LINT) fmt

fuzz:
	$(GO) test -mod=readonly ./internal/protocol -run='^$$' -fuzz='^FuzzRequest$$' -fuzztime=30s -parallel=2

fuzz-snapshot:
	@if [ "$$(uname -s)" = Linux ]; then \
		$(GO) test -mod=readonly ./internal/helper/linux -run='^$$' -fuzz='^FuzzLinuxSnapshot$$' -fuzztime=30s -parallel=2; \
	else \
		$(GO) test -mod=readonly ./internal/helper/darwin -run='^$$' -fuzz='^FuzzSnapshot$$' -fuzztime=30s -parallel=2; \
	fi

fuzz-dns:
	$(GO) test -mod=readonly ./internal/dnsrelay -run='^$$' -fuzz='^FuzzAnswer$$' -fuzztime=30s -parallel=2

fuzz-control:
	$(GO) test -mod=readonly ./internal/forwarder -run='^$$' -fuzz='^FuzzControl$$' -fuzztime=30s -parallel=2
