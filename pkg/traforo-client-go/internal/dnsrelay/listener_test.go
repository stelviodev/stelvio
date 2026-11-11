package dnsrelay

import (
	"errors"
	"net"
	"syscall"
	"testing"
	"time"
)

func TestListenerPairRetriesOnlyAddressCollisions(t *testing.T) {
	for _, scenario := range []struct {
		name      string
		failures  int
		fault     error
		wantCalls int
		success   bool
	}{
		{"first collision", 1, syscall.EADDRINUSE, 2, true},
		{"bounded collisions", 8, syscall.EADDRINUSE, 8, false},
		{"other failure", 1, syscall.EACCES, 1, false},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			var sockets []*packetFixture
			calls := 0
			udp, tcp, err := listenPair(func() (net.PacketConn, error) {
				c := &packetFixture{address: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 34000 + len(sockets)}}
				sockets = append(sockets, c)
				return c, nil
			}, func(address string) (net.Listener, error) {
				calls++
				if calls <= scenario.failures {
					return nil, scenario.fault
				}
				resolved, err := net.ResolveTCPAddr("tcp4", address)
				return &listenerFixture{address: resolved}, err
			})
			if calls != scenario.wantCalls {
				t.Fatalf("calls=%d", calls)
			}
			if scenario.success {
				if err != nil {
					t.Fatal(err)
				}
				if udp.LocalAddr().String() != tcp.Addr().String() {
					t.Fatal("mismatched ports")
				}
				_ = udp.Close()
				_ = tcp.Close()
			} else if !errors.Is(err, scenario.fault) {
				t.Fatal(err)
			}
			// Every UDP endpoint from a failed or completed pair must have been closed.
			for _, c := range sockets {
				if _, err := c.WriteTo([]byte{1}, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 53}); !errors.Is(err, net.ErrClosed) {
					t.Fatalf("leaked UDP socket: %v", err)
				}
			}
		})
	}
}

// Binding is not under test here. Keep the injected retry count deterministic
// even if a real ephemeral TCP port happens to be occupied by unrelated traffic.
type listenerFixture struct{ address net.Addr }

func (l *listenerFixture) Accept() (net.Conn, error) { return nil, net.ErrClosed }
func (l *listenerFixture) Close() error              { return nil }
func (l *listenerFixture) Addr() net.Addr            { return l.address }

type packetFixture struct {
	address net.Addr
	closed  bool
}

func (p *packetFixture) ReadFrom([]byte) (int, net.Addr, error) { return 0, nil, net.ErrClosed }
func (p *packetFixture) WriteTo([]byte, net.Addr) (int, error) {
	if p.closed {
		return 0, net.ErrClosed
	}
	return 0, nil
}
func (p *packetFixture) Close() error                     { p.closed = true; return nil }
func (p *packetFixture) LocalAddr() net.Addr              { return p.address }
func (p *packetFixture) SetDeadline(time.Time) error      { return nil }
func (p *packetFixture) SetReadDeadline(time.Time) error  { return nil }
func (p *packetFixture) SetWriteDeadline(time.Time) error { return nil }
