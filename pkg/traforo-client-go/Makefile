GO ?= go
GOLANGCI_LINT ?= golangci-lint

.PHONY: test race vet lint format fuzz fuzz-snapshot fuzz-dns fuzz-control

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
	$(GO) test -mod=readonly ./internal/helper/darwin -run='^$$' -fuzz='^FuzzSnapshot$$' -fuzztime=30s -parallel=2

fuzz-dns:
	$(GO) test -mod=readonly ./internal/dnsrelay -run='^$$' -fuzz='^FuzzAnswer$$' -fuzztime=30s -parallel=2

fuzz-control:
	$(GO) test -mod=readonly ./internal/forwarder -run='^$$' -fuzz='^FuzzControl$$' -fuzztime=30s -parallel=2
