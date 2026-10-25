//go:build linux

package helper

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"time"

	"github.com/godbus/dbus/v5"
	"github.com/stelviodev/traforo/internal/protocol"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

func names(s Snapshot, id uint32) (string, string, string) {
	stem := fmt.Sprintf("st%x%08x", s.Session[:2], id)
	return stem + "t", stem + "d", fmt.Sprintf("stelvio:%s:%x:%08x", s.Peer.Boot, s.Session, id)
}
func address(value uint32) net.IP {
	p := make(net.IP, 4)
	binary.BigEndian.PutUint32(p, value)
	return p
}
func mustAddr(ip net.IP) netip.Addr { a, _ := netip.AddrFromSlice(ip.To4()); return a }
func absent(e error) bool {
	var missing netlink.LinkNotFoundError
	return errors.As(e, &missing) || errors.Is(e, unix.ENODEV)
}
func ownedLink(name, alias, kind string, index int) (netlink.Link, error) {
	l, e := netlink.LinkByName(name)
	if e != nil {
		return nil, e
	}
	if l.Type() != kind || l.Attrs().Alias != alias || index != 0 && l.Attrs().Index != index {
		return nil, errUncertain
	}
	return l, nil
}
func createDNS(u *Unit) error {
	_, e := ownedLink(u.DNS, u.Alias, "dummy", u.DNSIndex)
	if absent(e) {
		l := &netlink.Dummy{LinkAttrs: netlink.LinkAttrs{Name: u.DNS, Alias: u.Alias}}
		e = netlink.LinkAdd(l)
		// Debian's kernel ignores IFLA_IFALIAS during dummy creation. Set it
		// explicitly, then verify it. A crash before this step retains uncertain
		// ownership rather than permitting deletion by name alone.
		if e == nil {
			e = netlink.LinkSetAlias(l, u.Alias)
		}
	}
	if e != nil {
		return e
	}
	l, e := ownedLink(u.DNS, u.Alias, "dummy", u.DNSIndex)
	if e != nil {
		return e
	}
	u.DNSIndex = l.Attrs().Index
	// resolved needs an IPv4-capable link to create its unicast DNS scope.
	ip := address(uint32(0xc0000281 + u.Slot))
	existing, err := netlink.AddrList(nil, netlink.FAMILY_V4)
	if err != nil {
		return err
	}
	for _, a := range existing {
		if a.IP.Equal(ip) && a.LinkIndex != u.DNSIndex {
			return errUncertain
		}
	}
	if err = netlink.AddrReplace(l, &netlink.Addr{IPNet: &net.IPNet{IP: ip, Mask: net.CIDRMask(32, 32)}, Flags: unix.IFA_F_NOPREFIXROUTE}); err != nil {
		return err
	}
	return netlink.LinkSetUp(l)
}
func createTUN(u *Unit) (int, error) {
	if _, e := netlink.LinkByName(u.TUN); e == nil {
		return -1, errUncertain
	} else if !absent(e) {
		return -1, e
	}
	fd, e := unix.Open("/dev/net/tun", unix.O_RDWR|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if e != nil {
		return -1, e
	}
	fail := func(e error) (int, error) { _ = unix.Close(fd); return -1, e }
	req, e := unix.NewIfreq(u.TUN)
	if e != nil {
		return fail(e)
	}
	req.SetUint16(unix.IFF_TUN | unix.IFF_NO_PI)
	if e = unix.IoctlIfreq(fd, unix.TUNSETIFF, req); e != nil {
		return fail(e)
	}
	l, e := netlink.LinkByName(u.TUN)
	if e != nil {
		return fail(e)
	}
	if e = netlink.LinkSetAlias(l, u.Alias); e != nil {
		return fail(e)
	}
	u.TUNIndex = l.Attrs().Index
	if e = netlink.LinkSetMTU(l, 1500); e != nil {
		return fail(e)
	}
	if e = netlink.AddrAdd(l, &netlink.Addr{IPNet: &net.IPNet{IP: address(u.Local), Mask: net.CIDRMask(32, 32)}, Peer: &net.IPNet{IP: address(u.Local + 1), Mask: net.CIDRMask(32, 32)}}); e != nil {
		return fail(e)
	}
	if e = netlink.LinkSetUp(l); e != nil {
		return fail(e)
	}
	for _, p := range u.Request.Ranges {
		_, dst, _ := net.ParseCIDR(p.String())
		route := netlink.Route{LinkIndex: u.TUNIndex, Dst: dst, Src: address(u.Local), Scope: netlink.SCOPE_LINK, Protocol: netlink.RouteProtocol(186), Table: unix.RT_TABLE_MAIN}
		if e = netlink.RouteAdd(&route); e != nil {
			return fail(e)
		}
	}
	return fd, nil
}
func routeConflict(r protocol.Request, ownIndex int) error {
	routes, e := netlink.RouteList(nil, netlink.FAMILY_V4)
	if e != nil {
		return e
	}
	if len(routes) > 16384 {
		return errUncertain
	}
	for _, route := range routes {
		if route.Dst == nil || route.LinkIndex == ownIndex {
			continue
		}
		bits, _ := route.Dst.Mask.Size()
		if bits == 0 {
			continue
		}
		for _, p := range r.Ranges {
			if route.Dst.Contains(net.ParseIP(p.Addr().String())) || p.Contains(mustAddr(route.Dst.IP)) {
				return fmt.Errorf("route %s conflicts with %s", p, route.Dst)
			}
		}
	}
	return nil
}

const resolvedService = "org.freedesktop.resolve1"
const managerPath = dbus.ObjectPath("/org/freedesktop/resolve1")

type dnsServer struct {
	Family  int32
	Address []byte
	Port    uint16
	Name    string
}
type dnsDomain struct {
	Name      string
	RouteOnly bool
}
type domainProperty struct {
	Index     int32
	Name      string
	RouteOnly bool
}

func bus() (*dbus.Conn, error) { return dbus.ConnectSystemBus() }
func call(c *dbus.Conn, method string, args ...any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return c.Object(resolvedService, managerPath).CallWithContext(ctx, resolvedService+".Manager."+method, 0, args...).Err
}
func domains(c *dbus.Conn) ([]domainProperty, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	var value dbus.Variant
	e := c.Object(resolvedService, managerPath).CallWithContext(ctx, "org.freedesktop.DBus.Properties.Get", 0, resolvedService+".Manager", "Domains").Store(&value)
	var result []domainProperty
	if e == nil {
		e = dbus.Store([]any{value.Value()}, &result)
	}
	if len(result) > 16384 {
		return nil, errUncertain
	}
	return result, e
}
func resolverConflict(u Unit) error {
	c, e := bus()
	if e != nil {
		return e
	}
	defer func() { _ = c.Close() }()
	values, e := domains(c)
	if e != nil {
		return e
	}
	for _, v := range values {
		if int(v.Index) == u.DNSIndex {
			continue
		}
		for _, r := range u.Request.Resolvers {
			if protocol.DNSOverlap(v.Name, r.Domain) {
				return fmt.Errorf("foreign resolver domain %s conflicts with %s", v.Name, r.Domain)
			}
		}
	}
	return nil
}
func configureDNS(u Unit) error {
	if len(u.Request.Resolvers) == 0 {
		return nil
	}
	// A journaled index can be recycled or its link can lose our ownership
	// marker. Never repair resolver settings using the index alone.
	if u.DNSIndex <= 0 {
		return errUncertain
	}
	if _, e := ownedLink(u.DNS, u.Alias, "dummy", u.DNSIndex); e != nil {
		return e
	}
	if e := resolverConflict(u); e != nil {
		return e
	}
	c, e := bus()
	if e != nil {
		return e
	}
	defer func() { _ = c.Close() }()
	port := u.Request.Resolvers[0].Port
	ds := []dnsDomain{}
	for _, r := range u.Request.Resolvers {
		if r.Port != port {
			return errors.New("one Linux DNS view must use one relay port")
		}
		ds = append(ds, dnsDomain{r.Domain, true})
	}
	i := int32(u.DNSIndex)
	for _, v := range []struct {
		method string
		args   []any
	}{
		{"SetLinkDefaultRoute", []any{i, false}},
		{"SetLinkLLMNR", []any{i, "no"}},
		{"SetLinkMulticastDNS", []any{i, "no"}},
		{"SetLinkDNSEx", []any{i, []dnsServer{{unix.AF_INET, []byte{127, 0, 0, 1}, port, ""}}}},
		{"SetLinkDomains", []any{i, ds}},
	} {
		if e = call(c, v.method, v.args...); e != nil {
			return e
		}
	}
	return verifyDNS(u)
}
func verifyDNS(u Unit) error {
	if len(u.Request.Resolvers) == 0 {
		return nil
	}
	if _, e := ownedLink(u.DNS, u.Alias, "dummy", u.DNSIndex); e != nil {
		return e
	}
	c, e := bus()
	if e != nil {
		return e
	}
	defer func() { _ = c.Close() }()
	// Read the link object, including the port, rather than trusting setter success.
	var path dbus.ObjectPath
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if e = c.Object(resolvedService, managerPath).CallWithContext(ctx, resolvedService+".Manager.GetLink", 0, int32(u.DNSIndex)).Store(&path); e != nil {
		return e
	}
	obj := c.Object(resolvedService, path)
	get := func(name string, out any) error {
		var v dbus.Variant
		e := obj.CallWithContext(ctx, "org.freedesktop.DBus.Properties.Get", 0, resolvedService+".Link", name).Store(&v)
		if e != nil {
			return e
		}
		return dbus.Store([]any{v.Value()}, out)
	}
	var servers []dnsServer
	var ds []dnsDomain
	var def bool
	if e = get("DNSEx", &servers); e != nil {
		return e
	}
	if e = get("Domains", &ds); e != nil {
		return e
	}
	if e = get("DefaultRoute", &def); e != nil {
		return e
	}
	if def || len(servers) != 1 || servers[0].Family != unix.AF_INET || !net.IP(servers[0].Address).Equal(net.IPv4(127, 0, 0, 1)) || servers[0].Port != u.Request.Resolvers[0].Port || servers[0].Name != "" || len(ds) != len(u.Request.Resolvers) {
		return errUncertain
	}
	for _, r := range u.Request.Resolvers {
		found := false
		for _, d := range ds {
			if d.Name == r.Domain && d.RouteOnly {
				found = true
			}
		}
		if !found {
			return errUncertain
		}
	}
	return nil
}
func deleteLink(name, alias, kind string, index int) error {
	l, e := ownedLink(name, alias, kind, index)
	if absent(e) {
		return nil
	}
	if e != nil {
		return e
	}
	return netlink.LinkDel(l)
}
func verifyRoutes(u Unit) error {
	routes, e := netlink.RouteList(nil, netlink.FAMILY_V4)
	if e != nil {
		return e
	}
	for _, p := range u.Request.Ranges {
		found := false
		for _, r := range routes {
			if r.Dst != nil && r.Dst.String() == p.String() && r.LinkIndex == u.TUNIndex && r.Protocol == 186 && r.Src.Equal(address(u.Local)) {
				found = true
			}
		}
		if !found {
			return errUncertain
		}
	}
	return nil
}

func resolvedOwner() (string, error) {
	c, e := bus()
	if e != nil {
		return "", e
	}
	defer func() { _ = c.Close() }()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	var owner string
	e = c.BusObject().CallWithContext(ctx, "org.freedesktop.DBus.GetNameOwner", 0, resolvedService).Store(&owner)
	return owner, e
}

func resourceConflict(u Unit, retained bool) error {
	if _, e := netlink.LinkByName(u.TUN); e == nil {
		return errUncertain
	} else if !absent(e) {
		return e
	}
	if retained {
		_, e := ownedLink(u.DNS, u.Alias, "dummy", u.DNSIndex)
		return e
	}
	if _, e := netlink.LinkByName(u.DNS); e == nil {
		return errUncertain
	} else if !absent(e) {
		return e
	}
	return nil
}
