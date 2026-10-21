//go:build darwin

package helper

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"net"
	"net/netip"

	"github.com/stelviodev/traforo/internal/darwin"
)

func network(p netip.Prefix) (uint32, uint32) {
	a := p.Addr().As4()
	return binary.BigEndian.Uint32(a[:]), uint32(math.MaxUint32) << (32 - p.Bits())
}

// routeConflictError records only fixed-width kernel inventory facts. Never
// include an unbounded interface name or command output in a privileged reply.
type routeConflictError struct {
	requestedNetwork, requestedMask uint32
	existing                        darwin.Route
}

func routeLabel(n, mask uint32) string {
	var raw [4]byte
	binary.BigEndian.PutUint32(raw[:], n)
	address := netip.AddrFrom4(raw)
	binary.BigEndian.PutUint32(raw[:], mask)
	if bits, width := net.IPMask(raw[:]).Size(); width == 32 {
		return netip.PrefixFrom(address, bits).Masked().String()
	}
	return fmt.Sprintf("%s mask %s", address, netip.AddrFrom4(raw))
}

func (e *routeConflictError) Error() string {
	return fmt.Sprintf("requested route %s conflicts with route %s on interface index %d",
		routeLabel(e.requestedNetwork, e.requestedMask), routeLabel(e.existing.Network, e.existing.Mask), e.existing.Index)
}
func (e *routeConflictError) Unwrap() error { return errUncertain }

func routeStatusIn(routes []darwin.Route, p netip.Prefix, index uint32) int {
	n, m := network(p)
	status := 0
	for _, r := range routes {
		if r.Mask == m && r.Network == n {
			if status != 0 || index == 0 || r.Index != index {
				return 2
			}
			status = 1
		}
	}
	return status
}

func routeConflict(n, m, index uint32) error {
	routes, err := darwin.Routes()
	if err != nil {
		return err
	}
	return routeConflictIn(routes, n, m, index)
}

func routeConflictIn(routes []darwin.Route, n, m, index uint32) error {
	for _, r := range routes {
		common := m & r.Mask
		if r.Mask != 0 && n&common == r.Network&common && (index == 0 || r.Index != index) {
			return &routeConflictError{requestedNetwork: n, requestedMask: m, existing: r}
		}
	}
	return nil
}

// routeTable keeps host calls local to this operation. Tests supply an owned
// table; production uses the Darwin device and a complete kernel inventory.
type routeTable struct {
	index   uint32
	read    func() ([]darwin.Route, error)
	add     func(uint32, uint32) error
	current func() bool
}

func (table routeTable) install(ranges []netip.Prefix) error {
	routes, err := table.read()
	if err != nil {
		return err
	}
	// Check the full destination set before adding any route.
	for _, p := range ranges {
		n, m := network(p)
		if err := routeConflictIn(routes, n, m, table.index); err != nil {
			return err
		}
	}
	for _, p := range ranges {
		routes, err = table.read()
		if err != nil {
			return err
		}
		status := routeStatusIn(routes, p, table.index)
		if status == 2 {
			n, m := network(p)
			if err := routeConflictIn(routes, n, m, table.index); err != nil {
				return err
			}
			return errUncertain
		}
		if status == 0 {
			n, m := network(p)
			if err := routeConflictIn(routes, n, m, table.index); err != nil {
				return err
			}
			if err := table.add(n, m); err != nil {
				return err
			}
		}
		routes, err = table.read()
		if err != nil {
			return errUncertain
		}
		n, m := network(p)
		if err := routeConflictIn(routes, n, m, table.index); err != nil {
			return err
		}
		if routeStatusIn(routes, p, table.index) != 1 || !table.current() {
			return errUncertain
		}
	}
	return table.verify(ranges)
}

func (table routeTable) verify(ranges []netip.Prefix) error {
	routes, err := table.read()
	if err != nil || !table.current() {
		return errUncertain
	}
	for _, p := range ranges {
		n, m := network(p)
		if err := routeConflictIn(routes, n, m, table.index); err != nil {
			return err
		}
		if routeStatusIn(routes, p, table.index) != 1 {
			return errUncertain
		}
	}
	return nil
}

func routesAbsent(routes []darwin.Route, ranges []netip.Prefix) bool {
	for _, p := range ranges {
		if routeStatusIn(routes, p, 0) != 0 {
			return false
		}
	}
	return true
}

// CF v1 retains its stage byte. An optional RC suffix contains five big-endian
// uint32 values: requested network/mask, conflicting network/mask, interface index.
// The fixed 26-byte payload fits the existing 256-byte reply bound.
func configureDiagnostic(stage byte, err error) []byte {
	body := []byte{'C', 'F', 1, stage}
	var conflict *routeConflictError
	if errors.As(err, &conflict) {
		body = append(body, 'R', 'C')
		for _, value := range []uint32{conflict.requestedNetwork, conflict.requestedMask, conflict.existing.Network, conflict.existing.Mask, conflict.existing.Index} {
			body = binary.BigEndian.AppendUint32(body, value)
		}
	}
	return body
}
