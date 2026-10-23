//go:build darwin

package forwarder

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/xjasonlyu/tun2socks/v2/core"
	"github.com/xjasonlyu/tun2socks/v2/core/device/iobased"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// The client uses bounded packet queues. The server is the actual production
// unit, including carrier framing, TCP stack, SOCKS handshake and stream copying.
// No host routes, root process or AWS resources are involved.
func TestTwoVPCLargeStreamsAndHalfClose(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	carrier, peer := pair(t)
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: carrier}
	defer func() {
		cancel()
		_ = carrier.Close()
		_ = peer.Close()
		for _, u := range m.units {
			u.close()
		}
	}()
	for id := uint32(1); id <= 2; id++ {
		proxy := streamProxy(t, byte(255-id))
		count := 20
		if id == 2 {
			count = 32
		}
		if err := m.apply(command{Operation: "add", Unit: id, Generation: 7, Attempt: 1, Proxy: proxy, CIDRs: subnetRanges(byte(255-id), count)}); err != nil {
			t.Fatal(err)
		}
	}
	var stacks []*stack.Stack
	var devices []*queuePackets
	defer func() {
		for _, s := range stacks {
			s.Close()
			s.Wait()
		}
	}()
	for id := uint32(1); id <= 2; id++ {
		d := &queuePackets{ctx: ctx, incoming: make(chan []byte, 2048), manager: m, id: id, generation: 7}
		m.writer.Lock()
		m.units[id].carrier = d
		m.writer.Unlock()
		devices = append(devices, d)
		ep, err := iobased.New(d, mtu, 0)
		if err != nil {
			t.Fatal(err)
		}
		s, err := core.CreateStack(&core.Config{LinkEndpoint: ep, TransportHandler: m.units[id]})
		if err != nil {
			t.Fatal(err)
		}
		stacks = append(stacks, s)
		// Endpoint shutdown must follow context cancellation, even on assertion failure.
		defer func() { cancel(); ep.Close(); ep.Wait() }()
		if err := s.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: tcpip.AddrFrom4([4]byte{192, 0, 2, byte(id)}).WithPrefix()}, stack.AddressProperties{}); err != nil {
			t.Fatal(err)
		}
	}
	transfer := func(id uint32) error {
		c, err := gonet.DialContextTCP(ctx, stacks[id-1], tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4([4]byte{10, byte(255 - id), 9, 19}), Port: 27017}, ipv4.ProtocolNumber)
		if err != nil {
			return err
		}
		defer func() { _ = c.Close() }()
		if err = c.SetDeadline(time.Now().Add(20 * time.Second)); err != nil {
			return err
		}
		payload := make([]byte, 1<<20)
		for block := 0; block < len(payload)/sha256.Size; block++ {
			digest := sha256.Sum256([]byte(fmt.Sprintf("vpc-%d-block-%d", id, block)))
			copy(payload[block*sha256.Size:], digest[:])
		}
		sent := make(chan error, 1)
		go func() {
			n, err := c.Write(payload)
			if err == nil && n != len(payload) {
				err = io.ErrShortWrite
			}
			if err == nil {
				err = c.CloseWrite()
			}
			sent <- err
		}()
		// Read concurrently with writes, retaining the original independent
		// proof's full-duplex behavior and the proxy's EOF-before-response rule.
		reply, err := io.ReadAll(io.LimitReader(c, int64(len(payload)+64)))
		if err != nil {
			cancel()
			_ = c.Close()
			writeErr := <-sent
			return fmt.Errorf("read %d bytes: %w", len(reply), errors.Join(err, writeErr))
		}
		if err := <-sent; err != nil {
			return err
		}
		want := append([]byte(fmt.Sprintf("vpc-%d\n", 255-id)), payload...)
		if !bytes.Equal(reply, want) {
			return fmt.Errorf("VPC %d: response differs (%d bytes)", id, len(reply))
		}
		return nil
	}
	results := make(chan error, 2)
	for id := uint32(1); id <= 2; id++ {
		go func(id uint32) { results <- transfer(id) }(id)
	}
	for range 2 {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	if err := transfer(2); err != nil {
		t.Fatalf("healthy VPC after removal: %v", err)
	}
	proxy := streamProxy(t, 254)
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 8, Attempt: 2, Proxy: proxy, CIDRs: subnetRanges(254, 20)}); err != nil {
		t.Fatal(err)
	}
	// Delayed ACKs can reach a newly published unit before this assignment.
	// Use the same writer lock as production unit.Write for carrier access.
	devices[0].generation = 8
	m.writer.Lock()
	m.units[1].carrier = devices[0]
	m.writer.Unlock()
	if err := transfer(1); err != nil {
		t.Fatalf("reconnected VPC: %v", err)
	}
	if err := transfer(2); err != nil {
		t.Fatalf("healthy VPC after reconnect: %v", err)
	}

	deadline := time.Now().Add(2 * time.Second)
	for (m.totals[1].Active.Load() != 0 || m.totals[2].Active.Load() != 0) && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if m.totals[1].Opened.Load() != 2 || m.totals[2].Opened.Load() != 3 {
		t.Fatalf("incorrect per-unit stream totals: %v %v", m.totals[1].snapshot(), m.totals[2].snapshot())
	}
	if m.totals[1].Active.Load() != 0 || m.totals[2].Active.Load() != 0 {
		t.Fatal("completed streams remain active")
	}
}

type queuePackets struct {
	ctx        context.Context
	incoming   chan []byte
	manager    *manager
	id         uint32
	generation uint64
}

func (d *queuePackets) Read(p []byte) (int, error) {
	select {
	case <-d.ctx.Done():
		return 0, io.EOF
	case packet := <-d.incoming:
		return copy(p, packet), nil
	}
}
func (d *queuePackets) Write(p []byte) (int, error) {
	d.manager.mu.Lock()
	u := d.manager.units[d.id]
	d.manager.mu.Unlock()
	if u == nil || !u.accepts(p, false) {
		return len(p), nil
	}
	select {
	case u.packets <- append([]byte(nil), p...):
		return len(p), nil
	case <-d.ctx.Done():
		return 0, io.EOF
	}
}
func (d *queuePackets) SetWriteDeadline(time.Time) error { return nil }
func (d *queuePackets) WriteMsgUnix(p, ancillary []byte, address *net.UnixAddr) (int, int, error) {
	if len(ancillary) != 0 || address != nil || len(p) < frameHeader || binary.BigEndian.Uint32(p) != d.id || binary.BigEndian.Uint64(p[4:]) != d.generation || binary.BigEndian.Uint32(p[12:]) != 2 {
		return 0, 0, fmt.Errorf("invalid or cross-VPC carrier frame")
	}
	select {
	case d.incoming <- append([]byte(nil), p[frameHeader:]...):
		return len(p), 0, nil
	case <-d.ctx.Done():
		return 0, 0, io.EOF
	}
}
func streamProxy(t *testing.T, octet byte) string {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	acceptDone := make(chan struct{})
	t.Cleanup(func() { _ = listener.Close(); <-acceptDone; workers.Wait() })
	go func() {
		defer close(acceptDone)
		for {
			c, err := listener.Accept()
			if err != nil {
				return
			}
			workers.Add(1)
			go func() {
				defer workers.Done()
				defer func() { _ = c.Close() }()
				if err := c.SetDeadline(time.Now().Add(25 * time.Second)); err != nil {
					return
				}
				greeting := make([]byte, 3)
				if _, err := io.ReadFull(c, greeting); err != nil {
					return
				}
				if !bytes.Equal(greeting, []byte{5, 1, 0}) {
					return
				}
				if _, err := c.Write([]byte{5, 0}); err != nil {
					return
				}
				target := make([]byte, 10)
				if _, err := io.ReadFull(c, target); err != nil {
					return
				}
				if !bytes.Equal(target, []byte{5, 1, 0, 1, 10, octet, 9, 19, 105, 137}) {
					return
				}
				if _, err := c.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0}); err != nil {
					return
				}
				data, err := io.ReadAll(io.LimitReader(c, (1<<20)+1))
				if err != nil || len(data) != 1<<20 {
					t.Errorf("SOCKS %d received %d bytes: %v", octet, len(data), err)
					return
				}
				if _, err := c.Write(append([]byte(fmt.Sprintf("vpc-%d\n", octet)), data...)); err != nil {
					return
				}
			}()
		}
	}()
	return listener.Addr().String()
}
