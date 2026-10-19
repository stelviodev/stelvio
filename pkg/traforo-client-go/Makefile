GO ?= go
GOLANGCI_LINT ?= golangci-lint

.PHONY: test race vet lint format fuzz

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
