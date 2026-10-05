//go:build darwin

// P0 forwarding proof: TCP and transport processes never run as root.
package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"golang.org/x/sys/unix"
	"io"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/xjasonlyu/tun2socks/v2/core"
	"github.com/xjasonlyu/tun2socks/v2/core/adapter"
	"github.com/xjasonlyu/tun2socks/v2/core/device/iobased"
	"github.com/xjasonlyu/tun2socks/v2/transport/socks5"
)

const protocol = "stelvio-vpc-proof/1"
const socketPath = "/private/var/run/stelvio-vpc-proof/broker.sock"

// The generic IO adapter expects plain IP. Darwin utun includes a 4-byte
// network-order family. Its Read count must exclude this prefix; outgoing
// packets need AF_INET, not the zeros emitted by the generic offset option.
type utunIO struct{ file io.ReadWriter }

func (u *utunIO) Read(packet []byte) (int, error) {
	raw := make([]byte, len(packet)+4)
	n, err := u.file.Read(raw)
	if err != nil {
		return 0, err
	}
	if n < 4 || binary.BigEndian.Uint32(raw[:4]) != syscall.AF_INET {
		return 0, nil
	}
	return copy(packet, raw[4:n]), nil
}
func (u *utunIO) Write(packet []byte) (int, error) {
	if len(packet) == 0 || packet[0]>>4 != 4 {
		return 0, errors.New("not IPv4")
	}
	raw := make([]byte, len(packet)+4)
	binary.BigEndian.PutUint32(raw[:4], syscall.AF_INET)
	copy(raw[4:], packet)
	n, err := u.file.Write(raw)
	if n < 4 {
		return 0, err
	}
	return n - 4, err
}

type entry struct {
	CIDR   string `json:"cidr"`
	SOCKS  string `json:"socks"`
	prefix netip.Prefix
}
type forwarding struct {
	entries     []entry
	ctx         context.Context
	slots       chan struct{}
	mu          sync.Mutex
	connections map[net.Conn]bool
}

func (f *forwarding) track(c net.Conn) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ctx.Err() != nil {
		c.Close()
		return false
	}
	f.connections[c] = true
	return true
}
func (f *forwarding) forget(c net.Conn) {
	c.Close()
	f.mu.Lock()
	delete(f.connections, c)
	f.mu.Unlock()
}
func (f *forwarding) close() {
	f.mu.Lock()
	defer f.mu.Unlock()
	for c := range f.connections {
		c.Close()
	}
}
func (f *forwarding) HandleUDP(c adapter.UDPConn) { c.Close() }
func (f *forwarding) HandleTCP(c adapter.TCPConn) {
	select {
	case f.slots <- struct{}{}:
	default:
		c.Close()
		return
	}
	go func() {
		defer func() { <-f.slots }()
		if !f.track(c) {
			return
		}
		defer f.forget(c)
		destination, err := netip.ParseAddrPort(c.LocalAddr().String())
		if err != nil {
			return
		}
		for _, vpc := range f.entries {
			if !vpc.prefix.Contains(destination.Addr()) {
				continue
			}
			ctx, cancel := context.WithTimeout(f.ctx, 10*time.Second)
			remote, err := dialSOCKS(ctx, vpc.SOCKS, destination.String())
			cancel()
			if err != nil {
				return
			}
			if !f.track(remote) {
				return
			}
			defer f.forget(remote)
			// Keep half-close semantics so a service can finish its response.
			done := make(chan struct{})
			var halfClosed sync.Once
			finish := func() {
				halfClosed.Do(func() {
					deadline := time.Now().Add(30 * time.Second)
					remote.SetDeadline(deadline)
					c.SetDeadline(deadline)
				})
			}
			go func() {
				io.Copy(remote, c)
				if half, ok := remote.(interface{ CloseWrite() error }); ok {
					half.CloseWrite()
				}
				finish()
				close(done)
			}()
			io.Copy(c, remote)
			if half, ok := c.(interface{ CloseWrite() error }); ok {
				half.CloseWrite()
			}
			finish()
			<-done
			return
		}
	}()
}

// Return the raw TCP socket after SOCKS negotiation. A generic net.Conn wrapper
// hides CloseWrite, causing deadlock for clients that FIN before the response.
func dialSOCKS(ctx context.Context, endpoint, destination string) (net.Conn, error) {
	c, err := (&net.Dialer{}).DialContext(ctx, "tcp", endpoint)
	if err != nil {
		return nil, err
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		c.Close()
		return nil, errors.New("SOCKS handshake requires deadline")
	}
	c.SetDeadline(deadline)
	if _, err := socks5.ClientHandshake(c, socks5.ParseAddrString(destination), socks5.CmdConnect, nil); err != nil {
		c.Close()
		return nil, err
	}
	c.SetDeadline(time.Time{})
	return c, nil
}

func validate(entries []entry) error {
	if len(entries) == 0 || len(entries) > 8 {
		return errors.New("require 1 through 8 VPC entries")
	}
	private := []netip.Prefix{netip.MustParsePrefix("10.0.0.0/8"), netip.MustParsePrefix("172.16.0.0/12"), netip.MustParsePrefix("192.168.0.0/16")}
	for i := range entries {
		e := &entries[i]
		prefix, err := netip.ParsePrefix(e.CIDR)
		if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() || prefix.String() != e.CIDR || prefix.Bits() < 16 || prefix.Bits() > 28 {
			return fmt.Errorf("invalid canonical private IPv4 CIDR: %q", e.CIDR)
		}
		allowed := false
		for _, p := range private {
			allowed = allowed || p.Contains(prefix.Addr())
		}
		if !allowed {
			return fmt.Errorf("non-RFC1918 CIDR: %q", e.CIDR)
		}
		e.prefix = prefix
		for j := 0; j < i; j++ {
			if prefix.Overlaps(entries[j].prefix) {
				return errors.New("overlapping VPC CIDRs")
			}
		}
		addr, err := netip.ParseAddrPort(e.SOCKS)
		if err != nil || addr.Addr() != netip.MustParseAddr("127.0.0.1") || addr.Port() < 1024 {
			return errors.New("SOCKS endpoint must be 127.0.0.1 on an unprivileged port")
		}
	}
	return nil
}
func descriptor(conn *net.UnixConn) (*os.File, string, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return nil, "", err
	}
	var peerErr error
	if err := raw.Control(func(fd uintptr) {
		cred, err := unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
		if err != nil {
			peerErr = err
			return
		}
		if cred.Uid != 0 {
			peerErr = errors.New("broker must authenticate as root")
		}
	}); err != nil {
		return nil, "", err
	}
	if peerErr != nil {
		return nil, "", peerErr
	}
	body, control := make([]byte, 128), make([]byte, syscall.CmsgSpace(4))
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	n, oob, flags, _, err := conn.ReadMsgUnix(body, control)
	if err != nil {
		return nil, "", err
	}
	if flags&(syscall.MSG_TRUNC|syscall.MSG_CTRUNC) != 0 {
		return nil, "", errors.New("truncated handshake")
	}
	messages, err := syscall.ParseSocketControlMessage(control[:oob])
	if err != nil || len(messages) != 1 {
		return nil, "", errors.New("invalid descriptor handshake")
	}
	fds, err := syscall.ParseUnixRights(&messages[0])
	if err != nil || len(fds) != 1 {
		return nil, "", errors.New("expected one descriptor")
	}
	file := os.NewFile(uintptr(fds[0]), "utun")
	fields := strings.Fields(string(body[:n]))
	if len(fields) != 2 || fields[0] != protocol || !strings.HasPrefix(fields[1], "utun") {
		file.Close()
		return nil, "", errors.New("incompatible broker")
	}
	return file, fields[1], nil
}

// Stand-ins for independent SSH SOCKS listeners. Each refuses the other's
// addresses and identifies its own path, beyond a successful TCP handshake.
func localSOCKS(ctx context.Context, prefix netip.Prefix, marker string) (string, error) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	go func() { <-ctx.Done(); listener.Close() }()
	go func() {
		for {
			c, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				c.SetDeadline(time.Now().Add(30 * time.Second))
				greeting := make([]byte, 3)
				if _, err := io.ReadFull(c, greeting); err != nil || string(greeting) != string([]byte{5, 1, 0}) {
					return
				}
				if _, err := c.Write([]byte{5, 0}); err != nil {
					return
				}
				request := make([]byte, 10)
				if _, err := io.ReadFull(c, request); err != nil || request[0] != 5 || request[1] != 1 || request[3] != 1 {
					return
				}
				target := netip.AddrFrom4([4]byte(request[4:8]))
				if !prefix.Contains(target) || binary.BigEndian.Uint16(request[8:]) != 8080 {
					return
				}
				if _, err := c.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0}); err != nil {
					return
				}
				if _, err := io.WriteString(c, marker+"\n"); err != nil {
					return
				}
				io.Copy(c, io.LimitReader(c, 2<<20))
			}()
		}
	}()
	return listener.Addr().String(), nil
}
func execute() error {
	local := flag.Bool("local", false, "use local test SOCKS servers for two VPCs")
	config := flag.String("config", "", "JSON array of {cidr,socks} for real SSH SOCKS listeners")
	flag.Parse()
	if os.Geteuid() == 0 {
		return errors.New("forwarder must run as the developer, never root")
	}
	if *local == (*config != "") {
		return errors.New("choose exactly one of --local or --config")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	var entries []entry
	if *local {
		for i, cidr := range []string{"10.254.0.0/16", "10.253.0.0/16"} {
			socks, err := localSOCKS(ctx, netip.MustParsePrefix(cidr), "vpc-"+strconv.Itoa(i+1))
			if err != nil {
				return err
			}
			entries = append(entries, entry{CIDR: cidr, SOCKS: socks})
		}
	} else {
		file, err := os.Open(*config)
		if err != nil {
			return err
		}
		defer file.Close()
		dec := json.NewDecoder(io.LimitReader(file, 16384))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&entries); err != nil {
			return err
		}
		var trailing any
		if err := dec.Decode(&trailing); err != io.EOF {
			return errors.New("trailing config data")
		}
	}
	if err := validate(entries); err != nil {
		return err
	}
	cidrs := make([]string, len(entries))
	for i, e := range entries {
		cidrs[i] = e.CIDR
	}
	fmt.Println("Start the installed broker in another terminal:")
	fmt.Printf("sudo /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof --serve %s\n", strings.Join(cidrs, " "))
	var conn *net.UnixConn
	deadline := time.Now().Add(90 * time.Second)
	for time.Now().Before(deadline) && ctx.Err() == nil {
		c, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: socketPath, Net: "unix"})
		if err == nil {
			conn = c
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
	if conn == nil {
		return errors.New("broker unavailable after 90s")
	}
	defer conn.Close()
	file, name, err := descriptor(conn)
	if err != nil {
		return err
	}
	defer file.Close()
	handler := &forwarding{entries: entries, ctx: ctx, slots: make(chan struct{}, 128), connections: make(map[net.Conn]bool)}
	defer handler.close()
	endpoint, err := iobased.New(&utunIO{file}, 1500, 0)
	if err != nil {
		return err
	}
	stack, err := core.CreateStack(&core.Config{LinkEndpoint: endpoint, TransportHandler: handler})
	if err != nil {
		return err
	}
	defer stack.Close()
	if _, err := conn.Write([]byte("OK")); err != nil {
		return err
	}
	conn.SetReadDeadline(time.Now().Add(time.Duration(len(entries)*10+15) * time.Second))
	ready, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil || ready != "READY\n" {
		return errors.New("broker did not confirm installed routes")
	}
	conn.SetReadDeadline(time.Time{})
	fmt.Printf("READY uid=%d interface=%s vpcs=%d\n", os.Getuid(), name, len(entries))
	if *local {
		fmt.Println("Run: .venv/bin/python spikes/dev-vpc-v1/probe.py")
	}
	lost := make(chan error, 1)
	go func() { b := make([]byte, 1); _, err := conn.Read(b); lost <- err }()
	select {
	case <-ctx.Done():
		return nil
	case err := <-lost:
		return fmt.Errorf("broker exited; closing descriptor: %v", err)
	}
}
func main() {
	if err := execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
