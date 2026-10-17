package forwarder

import (
	"bytes"
	"encoding/json"
	"fmt"
	"testing"
)

func TestDNSControlOwnershipAndReconnect(t *testing.T) {
	a, b := pair(t)
	defer a.Close()
	defer b.Close()
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
	if apply(add).Status != "ok" {
		t.Fatal("reconnect failed")
	}
	c.Attempt = 2
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
	if d.Decode(&hello) != nil || hello.Version != 2 || hello.DNS != 1 || hello.Status != "ready" {
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
	defer a.Close()
	defer b.Close()
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: a}
	defer m.closeDNS()
	defer func() {
		for _, u := range m.units {
			u.close()
		}
	}()
	for id := uint32(1); id <= 9; id++ {
		if r := m.control(command{Operation: "add", Unit: id, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); r.Status != "ok" {
			t.Fatal(r)
		}
		domain := fmt.Sprintf("vpc%d.test", id)
		r := m.control(command{Operation: "dns-configure", Unit: id, Generation: 1, Attempt: 1, Revision: 1, Resolver: "10.254.0.2", Domains: []string{domain}})
		if (id <= 8) != (r.Status == "ok") {
			t.Fatal("retained DNS unit limit", id, r)
		}
		if r = m.control(command{Operation: "remove", Unit: id, Generation: 1, Attempt: 1}); r.Status != "ok" {
			t.Fatal(r)
		}
	}
	if len(m.dns) != 8 {
		t.Fatal("unbounded retained views")
	}
}

func TestRetiredDNSRevisionCannotBeResurrected(t *testing.T) {
	a, b := pair(t)
	defer a.Close()
	defer b.Close()
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
