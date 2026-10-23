//go:build darwin

package helper

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/stelviodev/traforo/internal/diagnostics"
	"golang.org/x/sys/unix"
)

func randomEpoch() string {
	var value [16]byte
	_, _ = rand.Read(value[:])
	return hex.EncodeToString(value[:])
}

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

func (s *service) packetMetrics(unit uint32) *packetMetrics {
	if s.metrics == nil {
		s.metrics = make(map[uint32]*packetMetrics)
	}
	if s.metrics[unit] == nil {
		s.metrics[unit] = &packetMetrics{}
	}
	return s.metrics[unit]
}

func (s *service) diagnostics(uid uint32) map[string]any {
	value := map[string]any{"build_version": diagnostics.BuildVersion, "version": 1, "epoch": s.epoch, "sample_monotonic_ns": time.Since(s.started).Nanoseconds(), "pid": os.Getpid(), "owned": s.owned, "uncertain": s.uncertain, "mutating": s.working, "stopping": s.stopping}
	if s.owned && uid != s.peer.UID {
		value["details_available"] = false
		return value
	}
	value["details_available"] = true
	value["session_id"] = fmt.Sprintf("%x-%x-%x-%x-%x", s.session[:4], s.session[4:6], s.session[6:8], s.session[8:10], s.session[10:])
	value["owner_uid"] = s.peer.UID
	value["invalid_or_unknown_frames"] = s.invalidFrames
	value["last_failure"] = s.lastFailure
	value["journal_revision"] = s.state.Revision
	ownership := []any{}
	for index, u := range s.state.Units {
		files := []any{}
		for position, f := range u.Files {
			public := ""
			if position < len(u.Request.Resolvers) {
				public = spec(s.state, index, position).public
			}
			files = append(files, map[string]any{"name": public, "phase": f.Phase, "device": f.Device, "inode": f.Inode})
		}
		ownership = append(ownership, map[string]any{"unit": fmt.Sprintf("%08x", u.Request.Unit), "phase": u.Phase, "resolver_receipts": files})
	}
	value["journal_units"] = ownership
	value["cleanup_retry_scheduled"] = !s.cleanupAfter.IsZero()
	value["cleanup_backoff_seconds"] = s.cleanupBackoff.Seconds()
	units := []any{}
	for _, g := range s.grants {
		if g.phase == 0 {
			continue
		}
		ranges := []string{}
		for _, r := range g.request.Ranges {
			ranges = append(ranges, r.String())
		}
		resolvers := []any{}
		for _, r := range g.request.Resolvers {
			resolvers = append(resolvers, map[string]any{"domain": r.Domain, "port": r.Port})
		}
		units = append(units, map[string]any{"unit": fmt.Sprintf("%08x", g.request.Unit), "vpc_id": g.request.VPC, "generation": g.generation, "phase": g.phase, "active": g.active, "failed": g.failed, "interface_name": g.device.Name, "interface_index": g.device.Index, "ranges": ranges, "resolvers": resolvers, "counters": *s.packetMetrics(g.request.Unit)})
	}
	value["units"] = units
	return value
}
func (s *service) beginDiagnostics(c *client) {
	active := 0
	for _, other := range s.clients {
		if other.output != nil {
			active++
		}
	}
	if active >= 2 {
		replyClose(c.fd, Busy)
		c.fd = -1
		return
	}
	body, err := json.Marshal(s.diagnostics(c.peer.UID))
	if err != nil || len(body) > diagnostics.Limit {
		c.close()
		return
	}
	packet := make([]byte, 16+len(body))
	copy(packet, "STLVREP1")
	binary.BigEndian.PutUint32(packet[12:], uint32(len(body)))
	copy(packet[16:], body)
	c.output = packet
	c.deadline = time.Now().Add(500 * time.Millisecond)
	c.writeDiagnostics()
}
func (c *client) writeDiagnostics() {
	n, err := unix.SendmsgN(c.fd, c.output, nil, nil, unix.MSG_DONTWAIT)
	if err == unix.EAGAIN || err == unix.EINTR {
		return
	}
	if err != nil || n <= 0 {
		c.close()
		return
	}
	c.output = c.output[n:]
	if len(c.output) == 0 {
		c.close()
	}
}
