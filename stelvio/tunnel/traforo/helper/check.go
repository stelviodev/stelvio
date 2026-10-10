//go:build darwin

// Read-only fixture entrypoints; not reachable from the shipped executable.
package helper

import "stelvio.dev/traforo/protocol"

func ResolverDomain(name string, data []byte) (string, error) { return resolverDomain(name, data) }
func ResolverSpec(state Snapshot, i, j int) (string, []byte, error) {
	if state.Validate() != nil || i < 0 || i >= len(state.Units) || j < 0 || j >= len(state.Units[i].Files) {
		return "", nil, protocol.Invalid
	}
	s := spec(state, i, j)
	return s.public, s.contents, nil
}
func JournalIncomplete(header []byte, size uint64) bool { return incompleteHeader(header, size) }
