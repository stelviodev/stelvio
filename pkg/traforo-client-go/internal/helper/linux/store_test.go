//go:build linux

package helper

import (
	"encoding/json"
	"testing"

	"github.com/stelviodev/traforo/internal/protocol"
)

func TestLinuxJournalRejectsAmbiguousAndMalformedIdentity(t *testing.T) {
	s := Snapshot{Version: 1, Revision: 1, Peer: Owner{502, 200, 4, "00000000-0000-0000-0000-000000000001"}, Parent: Owner{502, 199, 3, "00000000-0000-0000-0000-000000000001"}, Session: [16]byte{1}, Capability: [16]byte{2}, Units: []Unit{}}
	data, e := json.Marshal(s)
	if e != nil {
		t.Fatal(e)
	}
	if _, _, e = decodeSnapshot(data, true); e != nil {
		t.Fatal(e)
	}
	bad := append([]byte(`{"version":1,`), data[1:]...)
	if _, _, e = decodeSnapshot(bad, true); e == nil {
		t.Fatal("accepted duplicate journal key")
	}
	s.Peer.UID = 0
	data, _ = json.Marshal(s)
	if _, _, e = decodeSnapshot(data, true); e == nil {
		t.Fatal("accepted root owner")
	}
	for _, data := range [][]byte{[]byte(`{}`), []byte(`[]`), []byte(`null`), []byte(`{} {}`), make([]byte, protocol.MaxJournal+1)} {
		if _, _, e = decodeSnapshot(data, true); e == nil {
			t.Fatal("accepted invalid journal")
		}
	}
}
func TestLinuxResourceNamesFenceSessionAndBoot(t *testing.T) {
	s := Snapshot{Peer: Owner{Boot: "boot"}, Session: [16]byte{1, 2, 3}}
	tun, dns, alias := names(s, 1)
	if len(tun) > 15 || len(dns) > 15 || tun == dns || alias == "" {
		t.Fatal("invalid link identities")
	}
	s.Session[15] = 9
	nextTun, _, nextAlias := names(s, 1)
	if tun != nextTun || alias == nextAlias {
		t.Fatal("full session must fence shortened names")
	}
}
func FuzzLinuxSnapshot(f *testing.F) {
	f.Add([]byte(`{}`))
	f.Add([]byte(`{"version":1,"version":2}`))
	f.Fuzz(func(t *testing.T, data []byte) { _, _, _ = decodeSnapshot(data, true) })
}
