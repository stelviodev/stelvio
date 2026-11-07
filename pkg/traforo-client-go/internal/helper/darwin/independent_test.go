//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"testing"

	"github.com/stelviodev/traforo/internal/protocol"
)

// These frozen vectors were independently encoded by the former Python harness.
// Do not generate their expected bytes using the Go encoder under test.
func TestIndependentVectors(t *testing.T) {
	data, e := os.ReadFile("../testdata/independent_vectors.json")
	if e != nil {
		t.Fatal(e)
	}
	var vectors []struct {
		Name, Kind string
		Args       []string
		Data       []byte
		Returncode int
		Stdout     []byte
	}
	if e = json.Unmarshal(data, &vectors); e != nil {
		t.Fatal(e)
	}
	for _, v := range vectors {
		t.Run(v.Name, func(t *testing.T) {
			accepted := false
			var out []byte
			switch v.Kind {
			case "request":
				r, e := protocol.Decode(v.Data)
				accepted = e == nil
				if accepted {
					scoped := r.Operation == protocol.Configure || r.Operation == protocol.Remove
					if r.KeepDNS != (r.Operation == protocol.Remove) {
						t.Fatal("retention scope")
					}
					if scoped && (r.Unit != 1 || r.Generation != 0x0102030405060708) {
						t.Fatal("unit/generation", r)
					}
					anonymous := r.Operation == protocol.Status || r.Operation == protocol.Reconcile || r.Operation == protocol.Diagnostics
					if !anonymous && hex.EncodeToString(r.Session[:]) != "38321ac9f08641ec91ce770956914537" {
						t.Fatal("session", r)
					}
					if r.Operation == protocol.Configure && (r.VPC != "vpc-0123456789abcdef0" || len(r.Ranges) != 2 || r.Ranges[0].String() != "10.254.0.0/16" || len(r.Resolvers) != 1 || r.Resolvers[0] != (protocol.Resolver{Domain: "db.private.test", Port: 10890})) {
						t.Fatal("configure fields", r)
					}
				}
			case "snapshot":
				if len(v.Args) > 0 && v.Args[0] == "--successor" {
					if len(v.Data) >= 8 {
						a, b := int(binary.BigEndian.Uint32(v.Data)), int(binary.BigEndian.Uint32(v.Data[4:]))
						if a+b+8 == len(v.Data) {
							before, e1 := DecodeSnapshot(v.Data[8 : 8+a])
							after, e2 := DecodeSnapshot(v.Data[8+a:])
							accepted = e1 == nil && e2 == nil && Successor(before, after)
						}
					}
				} else {
					state, e := DecodeSnapshot(v.Data)
					if e != nil && !reflect.DeepEqual(state, Snapshot{}) {
						t.Fatal("partial state adopted")
					}
					if len(v.Args) > 0 && v.Args[0] == "--resolver" {
						var i, j int
						if len(v.Args) == 3 {
							_, e1 := fmt.Sscan(v.Args[1], &i)
							_, e2 := fmt.Sscan(v.Args[2], &j)
							if e == nil && e1 == nil && e2 == nil && i >= 0 && j >= 0 && i < len(state.Units) && j < len(state.Units[i].Files) {
								spec := spec(state, i, j)
								out = append([]byte(spec.public+"\n"), spec.contents...)
								accepted = true
							}
						}
					} else if e == nil {
						out, e = EncodeSnapshot(state)
						accepted = e == nil
					}
				}
			case "journal":
				accepted = len(v.Data) >= 8 && incompleteHeader(v.Data[8:], binary.BigEndian.Uint64(v.Data))
			case "resolver":
				if len(v.Args) == 3 && v.Args[0] == "--overlap" {
					a, e1 := protocol.Domain(v.Args[1])
					b, e2 := protocol.Domain(v.Args[2])
					accepted = e1 == nil && e2 == nil && protocol.DNSOverlap(a, b)
				} else if len(v.Args) == 1 {
					name, e := resolverDomain(v.Args[0], v.Data)
					accepted = e == nil
					if accepted {
						out = []byte(name + "\n")
					}
				}
			case "packet":
				if len(v.Data) >= 9 {
					n := int(binary.BigEndian.Uint32(v.Data))
					if n <= len(v.Data)-9 {
						r, e := protocol.Decode(v.Data[9 : 9+n])
						p := v.Data[9+n:]
						accepted = e == nil && len(p) >= 4 && binary.BigEndian.Uint32(p) == protocol.IPv4 && protocol.PacketAllowed(p[4:], r, binary.BigEndian.Uint32(v.Data[4:]), v.Data[8] == 1)
					}
				}
			default:
				t.Fatal(v.Kind)
			}
			if accepted != (v.Returncode == 0) {
				t.Fatalf("accepted=%v expected status=%d", accepted, v.Returncode)
			}
			if len(v.Stdout) > 0 && !bytes.Equal(out, v.Stdout) {
				t.Fatalf("output differs: %q != %q", out, v.Stdout)
			}
		})
	}
}
