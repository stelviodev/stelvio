//go:build darwin

package forwarder

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/netip"
	"sync"
	"testing"
	"time"

	"github.com/miekg/dns"
	"github.com/stelviodev/traforo/internal/protocol"
)

func TestDNSControlOwnershipAndReconnect(t *testing.T) {
	a, b := pair(t)
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: a}
	defer m.closeDNS()
	defer func() {
		for _, u := range m.units {
			u.close()
		}
	}()
	commandID := uint64(0)
	apply := func(c command) reply {
		t.Helper()
		commandID++
		c.Version = 2
		c.ID = commandID
		wire, _ := json.Marshal(c)
		parsed, e := decodeCommand(wire)
		if e != nil {
			t.Fatal(e)
		}
		return m.control(parsed)
	}
	add := command{Operation: "add", Unit: 1, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}
	if apply(add).Status != "ok" {
		t.Fatal("packet add failed")
	}
	c := command{Operation: "dns-configure", Unit: 1, Generation: 1, Attempt: 1, Revision: 1, Resolver: "10.254.0.2", Domains: []string{"private.test"}}
	first := apply(c)
	if first.Status != "ok" || first.Port < 1024 {
		t.Fatalf("DNS configure: %+v", first)
	}
	if apply(c).Status != "error" {
		t.Fatal("stale DNS revision accepted")
	}
	for _, op := range []string{"dns-reject", "remove"} {
		if apply(command{Operation: op, Unit: 1, Generation: 1, Attempt: 1, Revision: map[bool]uint64{true: 1}[op == "dns-reject"]}).Status != "ok" {
			t.Fatal(op)
		}
	}
	add.Attempt = 2
	add.Generation = 2
	if apply(add).Status != "ok" {
		t.Fatal("reconnect failed")
	}
	c.Attempt = 2
	c.Generation = 2
	c.Revision = 2
	if next := apply(c); next.Status != "ok" || next.Port != first.Port {
		t.Fatal("listener did not survive reconnect")
	}
	for _, op := range []string{"dns-reject", "dns-retire", "dns-probe", "dns-arm", "dns-check", "dns-release"} {
		if apply(command{Operation: op, Unit: 1, Generation: 1, Attempt: 1, Revision: 1}).Status != "error" {
			t.Fatal("stale operation", op)
		}
	}
	add.Unit = 2
	add.CIDRs = []string{"10.253.0.0/16"}
	if apply(add).Status != "ok" {
		t.Fatal("second unit failed")
	}
	c.Unit = 2
	c.Resolver = "10.253.0.2"
	if apply(c).Status != "error" {
		t.Fatal("conflicting DNS ownership accepted")
	}
	c.Domains = []string{"other.test"}
	if apply(c).Status != "ok" {
		t.Fatal("independent DNS failed")
	}
	c.Unit = 1
	c.Resolver = "10.254.0.2"
	c.Domains = []string{"new.test"}
	c.Revision = 3
	if apply(c).Status != "error" {
		t.Fatal("ownership shrink accepted")
	}
}
func TestControlFramingAndSequence(t *testing.T) {
	m := &manager{}
	input := []byte("{\"version\":2,\"id\":1,\"operation\":\"unknown\",\"unit\":1,\"generation\":1,\"attempt\":1}\n")
	var output bytes.Buffer
	if e := m.serveControl(bytes.NewReader(append(input, input...)), &output); e != nil {
		t.Fatal(e)
	}
	d := json.NewDecoder(&output)
	var hello, first, duplicate reply
	if bytes.Contains(output.Bytes(), []byte(`"dns"`)) {
		t.Fatal("redundant DNS capability advertisement")
	}
	if d.Decode(&hello) != nil || hello.Version != 2 || hello.Status != "ready" {
		t.Fatal("missing protocol handshake")
	}
	if d.Decode(&first) != nil || d.Decode(&duplicate) != nil || first.ID != 1 || duplicate.Status != "error" {
		t.Fatal("request identity lost")
	}
	for _, wire := range []string{"{}", "null", string(input) + "{}", `{"version":2,"id":1,"unit":1,"generation":1,"attempt":1,"unknown":true}`} {
		if _, e := decodeCommand([]byte(wire)); e == nil {
			t.Fatal("invalid command admitted", wire)
		}
	}
}
func FuzzControl(f *testing.F) {
	for _, count := range []int{20, 32, 33} {
		wire, err := json.Marshal(command{Version: 2, ID: 1, Operation: "add", Unit: 1, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: subnetRanges(254, count)})
		if err != nil {
			f.Fatal(err)
		}
		f.Add(wire)
	}
	f.Add([]byte(`{"version":2,"id":1,"operation":"dns-probe","unit":1,"generation":1,"attempt":1,"revision":1}`))
	f.Fuzz(func(t *testing.T, wire []byte) {
		c, e := decodeCommand(wire)
		if e == nil && (c.Version != 2 || c.ID == 0 || c.Unit == 0 || c.Generation == 0 || c.Attempt == 0) {
			t.Fatal("unbounded identity")
		}
	})
}

func TestRetainedDNSViewsAreBounded(t *testing.T) {
	a, b := pair(t)
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: a}
	defer m.closeDNS()
	defer func() {
		for _, u := range m.units {
			u.close()
		}
	}()
	for id := uint32(1); id <= protocol.MaxUnits+1; id++ {
		r := m.control(command{Operation: "add", Unit: id, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}})
		if id > protocol.MaxUnits {
			if r.Status != "error" {
				t.Fatal("accepted unit beyond the retained history bound", r)
			}
			break
		}
		if r.Status != "ok" {
			t.Fatal(r)
		}
		domain := fmt.Sprintf("vpc%d.test", id)
		r = m.control(command{Operation: "dns-configure", Unit: id, Generation: 1, Attempt: 1, Revision: 1, Resolver: "10.254.0.2", Domains: []string{domain}})
		if r.Status != "ok" {
			t.Fatal("retained DNS unit limit", id, r)
		}
		if r = m.control(command{Operation: "remove", Unit: id, Generation: 1, Attempt: 1}); r.Status != "ok" {
			t.Fatal(r)
		}
	}
	if len(m.dns) != protocol.MaxUnits || len(m.unitGenerations) != protocol.MaxUnits {
		t.Fatal("unbounded retained views")
	}
}

func TestRetiredDNSRevisionCannotBeResurrected(t *testing.T) {
	a, b := pair(t)
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: a}
	defer m.closeDNS()
	defer func() {
		for _, u := range m.units {
			u.close()
		}
	}()
	if m.control(command{Operation: "add", Unit: 1, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}).Status != "ok" {
		t.Fatal("add")
	}
	c := command{Operation: "dns-configure", Unit: 1, Generation: 1, Attempt: 1, Revision: 10, Resolver: "10.254.0.2", Domains: []string{"private.test"}}
	if m.control(c).Status != "ok" {
		t.Fatal("configure")
	}
	if m.control(command{Operation: "dns-retire", Unit: 1, Generation: 1, Attempt: 1, Revision: 10}).Status != "ok" {
		t.Fatal("retire")
	}
	for _, revision := range []uint64{1, 10} {
		c.Revision = revision
		if m.control(c).Code != "stale" {
			t.Fatal("retirement erased revision fence", revision)
		}
	}
	c.Revision = 11
	if m.control(c).Status != "ok" {
		t.Fatal("new DNS revision refused")
	}
}

func TestRetiredDNSViewAllowsHigherGeneration(t *testing.T) {
	a, b := pair(t)
	defer func() { _ = a.Close(); _ = b.Close() }()
	m := &manager{carrier: a}
	defer m.close()
	apply := func(c command) reply {
		t.Helper()
		return m.control(c)
	}

	if r := apply(command{Operation: "add", Unit: 1, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); r.Status != "ok" {
		t.Fatal("initial packet add", r)
	}
	configure := command{Operation: "dns-configure", Unit: 1, Generation: 1, Attempt: 1, Revision: 10, Resolver: "10.254.0.2", Domains: []string{"private.test"}}
	if r := apply(configure); r.Status != "ok" {
		t.Fatal("initial DNS configure", r)
	}
	if r := apply(command{Operation: "dns-retire", Unit: 1, Generation: 1, Attempt: 1, Revision: 10}); r.Status != "ok" {
		t.Fatal("retire DNS view", r)
	}
	if r := apply(command{Operation: "remove", Unit: 1, Generation: 1, Attempt: 1}); r.Status != "ok" {
		t.Fatal("remove packet unit", r)
	}
	if r := apply(command{Operation: "add", Unit: 1, Generation: 2, Attempt: 2, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); r.Status != "ok" {
		t.Fatal("higher-generation packet add", r)
	}
	configure.Generation = 2
	configure.Attempt = 2
	configure.Revision = 11
	configure.Domains = []string{"next.private.test"}
	if r := apply(configure); r.Status != "ok" || r.Port < 1024 {
		t.Fatalf("higher-generation DNS configure = %+v", r)
	}
}

func TestDNSProbeDoesNotBlockCarrierDispatch(t *testing.T) {
	carrier, peer := pair(t)
	proxy, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	release := make(chan struct{})
	ready := make(chan struct{})
	serverDone := make(chan struct{})
	var serverErr error
	go func() {
		defer close(serverDone)
		c, err := proxy.Accept()
		if err != nil {
			serverErr = err
			return
		}
		defer func() { _ = c.Close() }()
		greeting := make([]byte, 3)
		if _, serverErr = io.ReadFull(c, greeting); serverErr != nil || string(greeting) != string([]byte{5, 1, 0}) {
			return
		}
		if _, serverErr = c.Write([]byte{5, 0}); serverErr != nil {
			return
		}
		request := make([]byte, 10)
		if _, serverErr = io.ReadFull(c, request); serverErr != nil || string(request[:8]) != string([]byte{5, 1, 0, 1, 10, 254, 0, 2}) {
			if serverErr == nil {
				serverErr = fmt.Errorf("unexpected SOCKS request: %x", request)
			}
			return
		}
		if _, serverErr = c.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0}); serverErr != nil {
			return
		}
		dc := &dns.Conn{Conn: c}
		query, err := dc.ReadMsg()
		if err != nil {
			serverErr = err
			return
		}
		close(ready)
		<-release
		answer := new(dns.Msg)
		answer.SetReply(query)
		serverErr = dc.WriteMsg(answer)
	}()

	ctx, cancel := context.WithCancel(context.Background())
	u := &unit{id: 1, generation: 1, cidrs: []netip.Prefix{netip.MustParsePrefix("10.254.0.0/16")}, packets: make(chan []byte, 1), ctx: ctx, cancel: cancel}
	u.proxy = proxy.Addr().String()
	m := &manager{
		units:           map[uint32]*unit{1: u},
		generations:     map[uint32]uint64{1: 1},
		unitGenerations: map[uint32]uint64{1: 1},
		carrier:         carrier,
	}
	if r := m.control(command{Operation: "dns-configure", Unit: 1, Generation: 1, Attempt: 1, Revision: 1, Resolver: "10.254.0.2", Domains: []string{"private.test"}, Proxy: ""}); r.Status != "ok" {
		t.Fatalf("DNS configure: %+v", r)
	}
	dispatchDone := make(chan struct{})
	go func() { defer close(dispatchDone); m.dispatch() }()
	probeDone := make(chan struct{})
	var probeReply reply
	go func() {
		probeReply = m.control(command{Operation: "dns-probe", Unit: 1, Generation: 1, Attempt: 1, Revision: 1})
		close(probeDone)
	}()
	closeOnce := sync.Once{}
	closeRelease := func() { closeOnce.Do(func() { close(release) }) }
	t.Cleanup(func() {
		closeRelease()
		_ = proxy.Close()
		_ = carrier.Close()
		_ = peer.Close()
		m.close()
		select {
		case <-dispatchDone:
		case <-time.After(time.Second):
			t.Error("carrier dispatcher did not stop during cleanup")
		}
		select {
		case <-probeDone:
		case <-time.After(5 * time.Second):
			t.Error("DNS probe did not stop during cleanup")
		}
		select {
		case <-serverDone:
		case <-time.After(5 * time.Second):
			t.Error("SOCKS fixture did not stop during cleanup")
		}
	})
	select {
	case <-ready:
	case <-time.After(2 * time.Second):
		t.Fatal("DNS probe did not reach the blocked upstream")
	}
	if _, err := peer.Write(syn(1, 1, 254, 45008)); err != nil {
		t.Fatal(err)
	}
	select {
	case packet := <-u.packets:
		if len(packet) != 40 || binary.BigEndian.Uint16(packet[20:22]) != 45008 {
			t.Fatalf("carrier dispatched unexpected packet: %x", packet)
		}
	case <-time.After(time.Second):
		t.Fatal("blocked DNS probe prevented carrier dispatch")
	}
	closeRelease()
	select {
	case <-probeDone:
		if probeReply.Status != "ok" {
			t.Fatalf("DNS probe reply: %+v", probeReply)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("DNS probe did not complete after upstream response")
	}
	select {
	case <-serverDone:
		if serverErr != nil {
			t.Fatalf("SOCKS fixture: %v", serverErr)
		}
	case <-time.After(time.Second):
		t.Fatal("SOCKS fixture did not finish")
	}
}
