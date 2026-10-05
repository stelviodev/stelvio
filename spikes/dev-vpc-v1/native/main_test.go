//go:build darwin

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"io"
	"net"
	"net/netip"
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

// The same packet adapter and forwarding boundary as the live runner, with
// bounded packet queues replacing utun. No routes, privilege, AWS or host DNS.
func TestTwoVPCPaths(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var entries []entry
	for i, cidr := range []string{"10.254.0.0/16", "10.253.0.0/16"} {
		marker := []string{"vpc-1", "vpc-2"}[i]
		socks, err := localSOCKS(ctx, netip.MustParsePrefix(cidr), marker)
		if err != nil {
			t.Fatal(err)
		}
		entries = append(entries, entry{CIDR: cidr, SOCKS: socks})
	}
	if err := validate(entries); err != nil {
		t.Fatal(err)
	}
	a, b := make(chan []byte, 128), make(chan []byte, 128)
	devices := []*testPackets{{a, b, ctx}, {b, a, ctx}}
	handler := &forwarding{entries: entries, ctx: ctx, slots: make(chan struct{}, 128), connections: make(map[net.Conn]bool)}
	defer handler.close()
	var stacks []*stack.Stack
	var endpoints []*iobased.Endpoint
	defer func() {
		cancel()
		handler.close()
		for _, s := range stacks {
			s.Close()
		}
		for _, endpoint := range endpoints {
			endpoint.Wait()
		}
	}()
	for _, device := range devices {
		ep, err := iobased.New(&utunIO{device}, 1500, 0)
		if err != nil {
			t.Fatal(err)
		}
		s, err := core.CreateStack(&core.Config{LinkEndpoint: ep, TransportHandler: handler})
		if err != nil {
			t.Fatal(err)
		}
		endpoints = append(endpoints, ep)
		stacks = append(stacks, s)
	}
	if err := stacks[0].AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: tcpip.AddrFrom4([4]byte{192, 0, 2, 1}).WithPrefix()}, stack.AddressProperties{}); err != nil {
		t.Fatal(err)
	}
	// Both VPCs concurrently, binary data larger than the stack window, and FIN
	// followed by a complete response. A swapped destination must fail markers.
	var workers sync.WaitGroup
	for i, target := range [][4]byte{{10, 254, 10, 10}, {10, 253, 20, 20}} {
		workers.Add(1)
		go func(i int, target [4]byte) {
			defer workers.Done()
			c, err := gonet.DialContextTCP(ctx, stacks[0], tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4(target), Port: 8080}, ipv4.ProtocolNumber)
			if err != nil {
				t.Error(err)
				return
			}
			defer c.Close()
			c.SetDeadline(time.Now().Add(20 * time.Second))
			payload := make([]byte, 1<<20)
			for block := 0; block < len(payload)/sha256.Size; block++ {
				var counter [8]byte
				binary.BigEndian.PutUint64(counter[:], uint64(block))
				digest := sha256.Sum256(counter[:])
				copy(payload[block*sha256.Size:], digest[:])
			}
			sent := make(chan error, 1)
			go func() {
				_, err := c.Write(payload)
				halfErr := c.CloseWrite()
				if err == nil {
					err = halfErr
				}
				sent <- err
			}()
			received, err := io.ReadAll(c)
			if err != nil {
				c.Close()
				<-sent
				t.Errorf("read: %v bytes=%d", err, len(received))
				return
			}
			if err := <-sent; err != nil {
				t.Error(err)
				return
			}
			expected := append([]byte([]string{"vpc-1\n", "vpc-2\n"}[i]), payload...)
			if !bytes.Equal(received, expected) {
				t.Errorf("VPC %d: got %d bytes, want %d with exact marker and payload", i+1, len(received), len(expected))
			}
		}(i, target)
	}
	workers.Wait()
}

// Packet queues replace the OS device, retaining datagram boundaries and the
// utun family header. Unlike Darwin AF_UNIX sockets they provide backpressure
// instead of ENOBUFS, and never require host routes or privilege.
type testPackets struct {
	incoming, outgoing chan []byte
	ctx                context.Context
}

func (d *testPackets) Read(b []byte) (int, error) {
	select {
	case <-d.ctx.Done():
		return 0, d.ctx.Err()
	case packet := <-d.incoming:
		if len(packet) > len(b) {
			return 0, io.ErrShortBuffer
		}
		return copy(b, packet), nil
	}
}
func (d *testPackets) Write(b []byte) (int, error) {
	packet := append([]byte(nil), b...)
	select {
	case <-d.ctx.Done():
		return 0, d.ctx.Err()
	case d.outgoing <- packet:
		return len(b), nil
	}
}
