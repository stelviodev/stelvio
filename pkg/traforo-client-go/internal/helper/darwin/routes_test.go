//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"testing"

	"github.com/stelviodev/traforo/internal/darwin"
	"github.com/stelviodev/traforo/internal/protocol"
	"golang.org/x/sys/unix"
)

func subnetPrefixes(count, offset int) []netip.Prefix {
	var ranges []netip.Prefix
	for i := 0; i < count; i++ {
		ranges = append(ranges, netip.MustParsePrefix(fmt.Sprintf("10.%d.%d.0/24", offset, i)))
	}
	return ranges
}
func TestSubnetRouteConflictPolicyAndDetails(t *testing.T) {
	n, mask := network(netip.MustParsePrefix("10.254.20.0/22"))
	for _, tc := range []struct {
		name     string
		route    darwin.Route
		index    uint32
		conflict bool
	}{
		{"local LAN outside destination", darwin.Route{Network: 0x0afe0000, Mask: 0xffffff00, Index: 7}, 42, false},
		{"default", darwin.Route{Index: 7}, 42, false},
		{"foreign exact", darwin.Route{Network: n, Mask: mask, Index: 7}, 42, true},
		{"foreign more specific", darwin.Route{Network: 0x0afe1500, Mask: 0xffffff00, Index: 7}, 42, true},
		{"foreign broader VPN", darwin.Route{Network: 0x0a000000, Mask: 0xff000000, Index: 7}, 42, true},
		{"owned exact", darwin.Route{Network: n, Mask: mask, Index: 42}, 42, false},
		{"unknown device", darwin.Route{Network: n, Mask: mask, Index: 42}, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := routeConflictIn([]darwin.Route{tc.route}, n, mask, tc.index)
			if !tc.conflict {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			var detail *routeConflictError
			if !errors.As(err, &detail) || !errors.Is(err, errUncertain) || detail.existing != tc.route || detail.requestedNetwork != n || detail.requestedMask != mask {
				t.Fatalf("lost conflict evidence: %v", err)
			}
			if !strings.Contains(err.Error(), "10.254.20.0/22") || !strings.Contains(err.Error(), "interface index") {
				t.Fatal(err)
			}
		})
	}
}
func TestSubnetRouteLifecycleIsIndependentAndIdempotent(t *testing.T) {
	var routes []darwin.Route
	first, second := subnetPrefixes(32, 10), subnetPrefixes(20, 11)
	newTable := func(index uint32) routeTable {
		return routeTable{index: index, read: func() ([]darwin.Route, error) { return routes, nil }, current: func() bool { return true }, add: func(n, m uint32) error {
			routes = append(routes, darwin.Route{Network: n, Mask: m, Index: index})
			return nil
		}}
	}
	for _, tc := range []struct {
		index  uint32
		ranges []netip.Prefix
	}{{42, first}, {43, second}, {42, first}} {
		if err := newTable(tc.index).install(tc.ranges); err != nil {
			t.Fatal(err)
		}
	}
	if len(routes) != 52 {
		t.Fatalf("retry created %d routes, want 52", len(routes))
	}
	// Simulate kernel removal after closing the first owned utun device.
	routes = append([]darwin.Route(nil), routes[32:]...)
	if !routesAbsent(routes, first) || routesAbsent(routes, second) {
		t.Fatal("independent retirement failed")
	}
	for _, route := range routes {
		if route.Index != 43 {
			t.Fatal("foreign unit changed")
		}
	}
	if err := newTable(44).install(first); err != nil {
		t.Fatal(err)
	}
	if len(routes) != 52 {
		t.Fatal("reconnect lost routes")
	}
	routes = nil
	if !routesAbsent(routes, first) || !routesAbsent(routes, second) {
		t.Fatal("baseline not restored")
	}
}
func TestRouteInstallPreflightAndPartialFailure(t *testing.T) {
	ranges := subnetPrefixes(20, 10)
	lastN, lastM := network(ranges[19])
	foreign := darwin.Route{Network: lastN, Mask: lastM, Index: 7}
	for _, fault := range []string{"conflict", "inventory", "add", "verification", "duplicate", "device"} {
		t.Run(fault, func(t *testing.T) {
			var routes []darwin.Route
			adds := 0
			if fault == "conflict" {
				routes = append(routes, foreign)
			}
			table := routeTable{index: 42, current: func() bool { return fault != "device" }, read: func() ([]darwin.Route, error) {
				if fault == "inventory" {
					return nil, errUncertain
				}
				return routes, nil
			}, add: func(n, m uint32) error {
				adds++
				if fault == "add" && adds == 2 {
					return errUncertain
				}
				if fault != "verification" {
					routes = append(routes, darwin.Route{Network: n, Mask: m, Index: 42})
				}
				if fault == "duplicate" {
					routes = append(routes, darwin.Route{Network: n, Mask: m, Index: 42})
				}
				return nil
			}}
			if err := table.install(ranges); err == nil {
				t.Fatal("uncertain installation succeeded")
			}
			if (fault == "conflict" || fault == "inventory") && adds != 0 {
				t.Fatal("preflight changed routes")
			}
			if fault == "conflict" && (len(routes) != 1 || routes[0] != foreign) {
				t.Fatal("foreign route changed")
			}
			if fault == "add" {
				if adds != 2 || len(routes) != 1 || routesAbsent(routes, ranges) {
					t.Fatal("partial ownership lost")
				}
				if err := table.install(ranges); err != nil {
					t.Fatal(err)
				}
				if len(routes) != 20 {
					t.Fatal("partial retry failed")
				}
			}
		})
	}
}
func TestForeignExactRoutePreventsCertifiedRemoval(t *testing.T) {
	ranges := subnetPrefixes(20, 10)
	n, m := network(ranges[19])
	foreign := []darwin.Route{{Network: n, Mask: m, Index: 7}}
	if routesAbsent(foreign, ranges) {
		t.Fatal("claimed removal despite foreign replacement")
	}
	if len(foreign) != 1 || foreign[0].Index != 7 {
		t.Fatal("foreign route changed")
	}
}
func TestRouteConflictDiagnosticIndependentBytes(t *testing.T) {
	err := &routeConflictError{requestedNetwork: 0x0afe1400, requestedMask: 0xfffffc00, existing: darwin.Route{Network: 0x0afe1500, Mask: 0xffffff00, Index: 7}}
	want, e := hex.DecodeString("4346011452430afe1400fffffc000afe1500ffffff0000000007")
	if e != nil {
		t.Fatal(e)
	}
	if got := configureDiagnostic(20, errors.Join(errors.New("bounded wrapper"), err)); !bytes.Equal(got, want) || len(got) != 26 {
		t.Fatalf("diagnostic %x, want %x", got, want)
	}
	if got := configureDiagnostic(5, errUncertain); !bytes.Equal(got, []byte{'C', 'F', 1, 5}) {
		t.Fatal("ordinary diagnostic changed")
	}
	if got := err.Error(); got != "requested route 10.254.20.0/22 conflicts with route 10.254.21.0/24 on interface index 7" {
		t.Fatal(got)
	}
}

func TestServiceReturnsBoundedRouteConflictWithoutDescriptors(t *testing.T) {
	s := serviceFixture()
	controls := servicePair(t, unix.SOCK_STREAM)
	conflict := &routeConflictError{requestedNetwork: 0x0afe1400, requestedMask: 0xfffffc00, existing: darwin.Route{Network: 0x0afe1500, Mask: 0xffffff00, Index: 7}}
	s.complete(jobResult{kind: protocol.Configure, stage: 20, err: conflict, client: client{fd: controls[0]}, state: s.state, devices: s.devices})
	poll := []unix.PollFd{{Fd: int32(controls[1]), Events: unix.POLLIN}}
	if n, err := unix.Poll(poll, 2000); err != nil || n != 1 {
		t.Fatalf("missing failure reply: %d %v", n, err)
	}
	data, control := make([]byte, 64), make([]byte, 128)
	n, oob, flags, _, err := unix.Recvmsg(controls[1], data, control, 0)
	want := make([]byte, 16)
	copy(want, "STLVREP1")
	binary.BigEndian.PutUint16(want[8:], Uncertain)
	binary.BigEndian.PutUint32(want[12:], 26)
	want = append(want, configureDiagnostic(20, conflict)...)
	if err != nil || oob != 0 || flags&unix.MSG_CTRUNC != 0 || !bytes.Equal(data[:n], want) {
		t.Fatalf("invalid failure reply %x: %v", data[:n], err)
	}
	assertClosed(t, controls[0])
}

func TestRouteInventoryRacesAreRefused(t *testing.T) {
	for _, fault := range []string{"new-narrower", "new-broader", "earlier-range"} {
		t.Run(fault, func(t *testing.T) {
			ranges := subnetPrefixes(2, 10)
			var routes []darwin.Route
			adds := 0
			foreign := darwin.Route{Network: 0x0a0a0080, Mask: 0xffffff80, Index: 7}
			if fault == "new-broader" {
				foreign.Network = 0x0a000000
				foreign.Mask = 0xff000000
			}
			table := routeTable{index: 42, read: func() ([]darwin.Route, error) { return routes, nil }, current: func() bool { return true }, add: func(n, m uint32) error {
				adds++
				routes = append(routes, darwin.Route{Network: n, Mask: m, Index: 42})
				if fault != "earlier-range" || adds == 2 {
					routes = append(routes, foreign)
				}
				return nil
			}}
			var conflict *routeConflictError
			if err := table.install(ranges); !errors.As(err, &conflict) || conflict.existing != foreign {
				t.Fatalf("missed concurrent conflict: %v", err)
			}
			if routes[len(routes)-1] != foreign {
				t.Fatal("foreign route changed")
			}
		})
	}
}

func TestFinalRouteVerificationRefusesNewOverlap(t *testing.T) {
	ranges := subnetPrefixes(2, 10)
	var routes []darwin.Route
	for _, prefix := range ranges {
		n, m := network(prefix)
		routes = append(routes, darwin.Route{Network: n, Mask: m, Index: 42})
	}
	table := routeTable{index: 42, read: func() ([]darwin.Route, error) { return routes, nil }, current: func() bool { return true }}
	if err := table.verify(ranges); err != nil {
		t.Fatal(err)
	}
	routes = append(routes, darwin.Route{Network: 0x0a0a0080, Mask: 0xffffff80, Index: 7})
	var conflict *routeConflictError
	if err := table.verify(ranges); !errors.As(err, &conflict) {
		t.Fatalf("published readiness despite overlap: %v", err)
	}
}
