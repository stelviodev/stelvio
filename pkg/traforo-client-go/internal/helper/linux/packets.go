//go:build linux

package helper

import (
	"encoding/binary"
	"fmt"
	"os"
	"time"

	"github.com/stelviodev/traforo/internal/diagnostics"
	"github.com/stelviodev/traforo/internal/protocol"
	"golang.org/x/sys/unix"
)

type packetMetrics struct {
	HostReceived         uint64 `json:"host_received_packets"`
	HostBytesReceived    uint64 `json:"host_received_bytes"`
	HostDelivered        uint64 `json:"host_delivered_packets"`
	HostBytesDelivered   uint64 `json:"host_delivered_bytes"`
	ReturnReceived       uint64 `json:"return_received_packets"`
	ReturnBytesReceived  uint64 `json:"return_received_bytes"`
	ReturnDelivered      uint64 `json:"return_delivered_packets"`
	ReturnBytesDelivered uint64 `json:"return_delivered_bytes"`
	StaleGeneration      uint64 `json:"stale_generation_drops"`
	DestinationRejected  uint64 `json:"destination_rejected_drops"`
	CarrierDropped       uint64 `json:"carrier_backpressure_drops"`
	CarrierErrors        uint64 `json:"carrier_write_error_drops"`
	DeviceWriteDropped   uint64 `json:"device_write_drops"`
	DeviceErrors         uint64 `json:"device_error_drops"`
}

func (s *service) counters(id uint32) *packetMetrics {
	if s.metrics[id] == nil {
		s.metrics[id] = &packetMetrics{}
	}
	return s.metrics[id]
}
func (s *service) pumpHost(u Unit) {
	fd := s.devices[u.Slot]
	if fd < 0 {
		return
	}
	for attempt := 0; attempt < 32; attempt++ {
		packet := make([]byte, 1501)
		n, e := unix.Read(fd, packet)
		if e == unix.EAGAIN || e == unix.EINTR {
			return
		}
		m := s.counters(u.Request.Unit)
		if e != nil || n == 0 {
			m.DeviceErrors++
			return
		}
		m.HostReceived++
		m.HostBytesReceived += uint64(n)
		if !protocol.PacketAllowed(packet[:n], u.Request, u.Local, true) {
			m.DestinationRejected++
			continue
		}
		wire := make([]byte, 16+n)
		binary.BigEndian.PutUint32(wire, u.Request.Unit)
		binary.BigEndian.PutUint64(wire[4:], u.Request.Generation)
		binary.BigEndian.PutUint32(wire[12:], protocol.IPv4)
		copy(wire[16:], packet[:n])
		sent, e := unix.SendmsgN(s.carrier, wire, nil, nil, unix.MSG_DONTWAIT|unix.MSG_NOSIGNAL)
		if e == unix.EAGAIN || e == unix.ENOBUFS {
			m.CarrierDropped++
		} else if e != nil || sent != len(wire) {
			m.CarrierErrors++
		} else {
			m.HostDelivered++
			m.HostBytesDelivered += uint64(n)
		}
	}
}
func (s *service) pumpReturn() {
	for attempt := 0; attempt < 64; attempt++ {
		wire := make([]byte, 1517)
		control := make([]byte, 4096)
		n, count, flags, _, e := unix.Recvmsg(s.carrier, wire, control, unix.MSG_DONTWAIT|unix.MSG_CMSG_CLOEXEC)
		if e == unix.EAGAIN || e == unix.EINTR {
			return
		}
		if e != nil {
			return
		}
		if count > 0 {
			messages, _ := unix.ParseSocketControlMessage(control[:count])
			for _, message := range messages {
				fds, _ := unix.ParseUnixRights(&message)
				for _, fd := range fds {
					_ = unix.Close(fd)
				}
			}
		}
		if count != 0 || flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0 {
			continue
		}
		frame, e := protocol.DecodeFrame(wire[:n])
		if e != nil || binary.BigEndian.Uint32(wire[12:]) != protocol.IPv4 {
			continue
		}
		for _, u := range s.state.Units {
			if u.Request.Unit != frame.Unit {
				continue
			}
			m := s.counters(frame.Unit)
			m.ReturnReceived++
			m.ReturnBytesReceived += uint64(n - 16)
			if u.Phase != 2 || u.Request.Generation != frame.Generation {
				m.StaleGeneration++
				break
			}
			if !protocol.PacketAllowed(wire[16:n], u.Request, u.Local, false) {
				m.DestinationRejected++
				break
			}
			sent, e := unix.Write(s.devices[u.Slot], wire[16:n])
			if e == unix.EAGAIN || e == unix.EINTR {
				m.DeviceWriteDropped++
			} else if e != nil || sent != n-16 {
				m.DeviceErrors++
			} else {
				m.ReturnDelivered++
				m.ReturnBytesDelivered += uint64(sent)
			}
			break
		}
	}
}
func (s *service) diagnostics(uid uint32) map[string]any {
	value := map[string]any{"build_version": diagnostics.BuildVersion, "version": 1, "epoch": s.epoch, "sample_monotonic_ns": time.Since(s.started).Nanoseconds(), "pid": os.Getpid(), "owned": s.owned, "uncertain": s.uncertain, "mutating": false, "stopping": s.stopping, "details_available": !s.owned || uid == s.state.Peer.UID}
	if s.owned && uid != s.state.Peer.UID {
		return value
	}
	session := s.state.Session
	value["session_id"] = fmt.Sprintf("%x-%x-%x-%x-%x", session[:4], session[4:6], session[6:8], session[8:10], session[10:])
	value["owner_uid"] = s.state.Peer.UID
	value["journal_revision"] = s.state.Revision
	value["last_failure"] = s.lastFailure
	units := []any{}
	for _, u := range s.state.Units {
		ranges := []string{}
		for _, r := range u.Request.Ranges {
			ranges = append(ranges, r.String())
		}
		resolvers := []any{}
		for _, r := range u.Request.Resolvers {
			resolvers = append(resolvers, map[string]any{"domain": r.Domain, "port": r.Port})
		}
		units = append(units, map[string]any{"unit": fmt.Sprintf("%08x", u.Request.Unit), "vpc_id": u.Request.VPC, "generation": u.Request.Generation, "phase": u.Phase, "active": u.Phase == 2, "failed": u.Phase == 4, "interface_name": u.TUN, "interface_index": u.TUNIndex, "dns_interface_name": u.DNS, "ranges": ranges, "resolvers": resolvers, "counters": *s.counters(u.Request.Unit)})
	}
	value["units"] = units
	return value
}
