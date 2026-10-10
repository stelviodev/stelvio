//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"math"
	"net/netip"
	"reflect"
	"testing"
	"time"

	"github.com/stelviodev/traforo/darwin"
	"github.com/stelviodev/traforo/protocol"
	"golang.org/x/sys/unix"
)

func serviceFixture() service {
	peer := darwin.Peer{UID: 502, PID: 1234, Seconds: 100, Micros: 10, Token: [8]uint32{0, 502, 20, 502, 20, 1234, 0, 1}}
	session, capability := [16]byte{1}, [16]byte{2}
	r := protocol.Request{Operation: protocol.Configure, Session: session, Capability: capability, Unit: 1, Generation: 3, VPC: "vpc-12345678", Ranges: []netip.Prefix{netip.MustParsePrefix("10.254.0.0/16")}, Resolvers: []protocol.Resolver{{Domain: "db.internal", Port: 5300}}}
	s := service{gate: -1, listener: -1, lease: -1, carrier: -1, owned: true, peer: peer, session: session, capability: capability, devices: initDevices(), completion: make(chan jobResult, 1)}
	s.state = Snapshot{Revision: 1, Peer: peer, Session: session, Capability: capability, Carrier: 1, Units: []Unit{{Phase: Active, Generation: 3, Request: r, Packet: []byte("independent original configuration"), Index: 42, Files: []Receipt{{Phase: 2, Device: 22, Inode: 33}}}}}
	return s
}

func TestServiceGenerationAndRetentionAdmission(t *testing.T) {
	cases := []struct {
		name                                                  string
		phase                                                 byte
		current, requested                                    uint64
		configure, keep, oldKeep, exact, extend, absent, want bool
	}{
		{name: "new unit", configure: true, requested: 1, absent: true, want: true},
		{name: "maximal configure", configure: true, requested: math.MaxUint64, absent: true},
		{name: "active exact retry", phase: Active, current: 3, requested: 3, configure: true, exact: true, want: true},
		{name: "active changed grant", phase: Active, current: 3, requested: 3, configure: true},
		{name: "active dns extension", phase: Active, current: 3, requested: 3, configure: true, extend: true, want: true},
		{name: "extension stale generation", phase: Active, current: 3, requested: 2, configure: true, extend: true},
		{name: "preparing exact retry", phase: Preparing, current: 3, requested: 3, configure: true, exact: true, want: true},
		{name: "retained configure", phase: Retained, current: 4, requested: 5, configure: true},
		{name: "removed next configure", phase: Removed, current: 4, requested: 5, configure: true, want: true},
		{name: "removed same configure", phase: Removed, current: 4, requested: 4, configure: true},
		{name: "missing removal", requested: 4, absent: true},
		{name: "active next removal", phase: Active, current: 3, requested: 4, want: true},
		{name: "active stale removal", phase: Active, current: 3, requested: 3},
		{name: "retained retry", phase: Retained, current: 4, requested: 4, keep: true, oldKeep: true, want: true},
		{name: "retained stale", phase: Retained, current: 4, requested: 3, keep: true, oldKeep: true},
		{name: "retained final cleanup", phase: Retained, current: 4, requested: 5, oldKeep: true, want: true},
		{name: "removing exact retry", phase: Removing, current: 4, requested: 4, want: true},
		{name: "removing retention retry", phase: Removing, current: 4, requested: 4, keep: true, oldKeep: true, want: true},
		{name: "same generation retention change", phase: Removing, current: 4, requested: 4, oldKeep: true},
		{name: "abandon retention new generation", phase: Removing, current: 4, requested: 5, oldKeep: true, want: true},
		{name: "start retention while removing", phase: Removing, current: 4, requested: 5, keep: true},
		{name: "removed retry", phase: Removed, current: 4, requested: 4, want: true},
		{name: "removed advanced retry", phase: Removed, current: 4, requested: 5},
		{name: "maximal retention", phase: Active, current: 3, requested: math.MaxUint64, keep: true},
		{name: "maximal final cleanup", phase: Active, current: 3, requested: math.MaxUint64, want: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := serviceFixture()
			u := &s.state.Units[0]
			u.Phase = tc.phase
			u.Generation = tc.current
			u.KeepDNS = tc.oldKeep
			r := u.Request
			r.Generation = tc.requested
			r.KeepDNS = tc.keep
			if tc.configure {
				r.Operation = protocol.Configure
			} else {
				r.Operation = protocol.Remove
			}
			if tc.extend {
				r.Resolvers = append(append([]protocol.Resolver(nil), r.Resolvers...), protocol.Resolver{Domain: "cache.internal", Port: 5300})
			}
			c := client{data: []byte("changed configuration")}
			if tc.exact {
				c.data = append([]byte(nil), u.Packet...)
			}
			if tc.absent {
				s.state.Units = nil
			}
			if got := s.validChange(c, r); got != tc.want {
				t.Fatalf("accepted=%v want=%v", got, tc.want)
			}
		})
	}
}

func servicePair(t *testing.T, kind int) [2]int {
	t.Helper()
	fds, err := unix.Socketpair(unix.AF_UNIX, kind, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { unix.Close(fds[0]); unix.Close(fds[1]) })
	return fds
}

func assertClosed(t *testing.T, fd int) {
	t.Helper()
	if _, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); err != unix.EBADF {
		t.Fatalf("descriptor %d remains open: %v", fd, err)
	}
}

func assertReply(t *testing.T, fd int, status uint16) {
	t.Helper()
	poll := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
	if n, err := unix.Poll(poll, 2000); err != nil || n != 1 {
		t.Fatalf("reply missing: n=%d err=%v", n, err)
	}
	data := make([]byte, 64)
	control := make([]byte, 128)
	n, oob, flags, _, err := unix.Recvmsg(fd, data, control, 0)
	want := make([]byte, 16)
	copy(want, "STLVREP1")
	binary.BigEndian.PutUint16(want[8:], status)
	if err != nil || oob != 0 || flags&unix.MSG_CTRUNC != 0 || !bytes.Equal(data[:n], want) {
		t.Fatalf("reply=%x controls=%d flags=%d err=%v want=%x", data[:n], oob, flags, err, want)
	}
}

func TestServiceRejectsUnauthorizedRequestsWithoutMutatingLease(t *testing.T) {
	for _, operation := range []byte{protocol.Configure, protocol.Remove, protocol.Release} {
		for _, violation := range []string{"peer", "birth", "session", "capability", "revoked", "unowned", "unleased"} {
			t.Run(string(rune('0'+operation))+"/"+violation, func(t *testing.T) {
				s := serviceFixture()
				lease := servicePair(t, unix.SOCK_STREAM)
				carrier := servicePair(t, unix.SOCK_DGRAM)
				request := servicePair(t, unix.SOCK_STREAM)
				s.lease = lease[0]
				s.carrier = carrier[0]
				s.grants[0] = grant{active: true, phase: Active, generation: 3, request: s.state.Units[0].Request}
				c := client{fd: request[0], peer: s.peer}
				r := s.state.Units[0].Request
				r.Operation = operation
				r.Generation = 4
				switch violation {
				case "peer":
					c.peer.UID++
				case "birth":
					c.peer.Micros++
				case "session":
					r.Session[0]++
				case "capability":
					r.Capability[0]++
				case "revoked":
					s.revoke = true
				case "unowned":
					s.owned = false
				case "unleased":
					s.lease = -1
				}
				before := s.state.Clone()
				grants := s.grants
				beforeLease, beforeCarrier := s.lease, s.carrier
				s.handle(&c, r)
				assertReply(t, request[1], Unauthorized)
				assertClosed(t, request[0])
				if c.fd != -1 || s.working || !reflect.DeepEqual(before, s.state) || !reflect.DeepEqual(s.grants, grants) || s.lease != beforeLease || s.carrier != beforeCarrier {
					t.Fatal("unauthorized request mutated lease or state")
				}
			})
		}
	}
}

func TestRevocationClosesCarrierAndLeaseWithoutErasingRecoveryState(t *testing.T) {
	s := serviceFixture()
	lease := servicePair(t, unix.SOCK_STREAM)
	carrier := servicePair(t, unix.SOCK_DGRAM)
	s.lease = lease[0]
	s.carrier = carrier[0]
	s.grants[0].active = true
	before := s.state.Clone()
	s.revokeLease()
	assertClosed(t, lease[0])
	assertClosed(t, carrier[0])
	s.revokeLease()
	if s.lease != -1 || s.carrier != -1 || !s.revoke || s.grants[0].active || !s.owned || !reflect.DeepEqual(before, s.state) {
		t.Fatal("revocation lost recovery evidence or retained authority")
	}
	if _, err := unix.Write(carrier[1], []byte("revoked packet")); err == nil {
		t.Fatal("revoked carrier still accepts packets")
	}
}

func TestReleaseRevokesImmediatelyAndRetainsFailedCleanupEvidence(t *testing.T) {
	s := serviceFixture()
	lease := servicePair(t, unix.SOCK_STREAM)
	carrier := servicePair(t, unix.SOCK_DGRAM)
	control := servicePair(t, unix.SOCK_STREAM)
	gate, err := unix.Open("/dev/null", unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(gate)
	s.gate = gate
	s.lease = lease[0]
	s.carrier = carrier[0]
	s.store = Store{Image: -1, Lease: -1}
	c := client{fd: control[0], peer: s.peer}
	r := protocol.Request{Operation: protocol.Release, Session: s.session, Capability: s.capability}
	s.handle(&c, r)
	assertClosed(t, lease[0])
	assertClosed(t, carrier[0])
	if !s.working || c.fd != -1 || !s.revoke {
		t.Fatal("release did not transfer response ownership and revoke immediately")
	}
	// The real worker cannot certify this deliberately uninstalled store. It must
	// finish without root/host effects and preserve uncertain recovery ownership.
	s.stopping = true
	select {
	case j := <-s.completion:
		if j.err == nil || !j.cacheError {
			t.Fatal("uncertifiable cleanup succeeded")
		}
		s.complete(j)
	case <-time.After(3 * time.Second):
		t.Fatal("cleanup worker did not complete")
	}
	assertReply(t, control[1], Uncertain)
	assertClosed(t, control[0])
	if s.working || !s.owned || !s.uncertain || !s.revoke || s.lease != -1 || s.carrier != -1 || len(s.state.Units) != 1 {
		t.Fatal("failed cleanup lost ownership or restored a lease")
	}
}

func TestStoppingAcquireCompletionCannotRenewAuthority(t *testing.T) {
	s := serviceFixture()
	s.stopping = true
	s.working = true
	s.revoke = true
	control := servicePair(t, unix.SOCK_STREAM)
	gate := servicePair(t, unix.SOCK_STREAM)
	s.gate = gate[0]
	s.complete(jobResult{kind: protocol.Acquire, client: client{fd: control[0], peer: s.peer}, state: Snapshot{}, devices: initDevices()})
	assertReply(t, control[1], OK)
	assertClosed(t, control[0])
	assertClosed(t, gate[0])
	if s.working || s.owned || s.uncertain || s.lease != -1 || s.carrier != -1 || s.session != ([16]byte{}) || s.capability != ([16]byte{}) {
		t.Fatal("completion during shutdown restored authority")
	}
}

func TestServiceBusyAndStoppingRejectNewMutations(t *testing.T) {
	for _, mode := range []string{"working", "stopping"} {
		for _, operation := range []byte{protocol.Acquire, protocol.Configure, protocol.Remove, protocol.Release, protocol.Reconcile} {
			t.Run(mode+"/"+string(rune('0'+operation)), func(t *testing.T) {
				s := serviceFixture()
				s.working = mode == "working"
				s.stopping = mode == "stopping"
				control := servicePair(t, unix.SOCK_STREAM)
				c := client{fd: control[0], peer: s.peer}
				r := s.state.Units[0].Request
				r.Operation = operation
				before := s.state.Clone()
				s.handle(&c, r)
				assertReply(t, control[1], Busy)
				assertClosed(t, control[0])
				if c.fd != -1 || s.working != (mode == "working") || !reflect.DeepEqual(s.state, before) {
					t.Fatal("busy request changed worker or recovery state")
				}
			})
		}
	}
}
