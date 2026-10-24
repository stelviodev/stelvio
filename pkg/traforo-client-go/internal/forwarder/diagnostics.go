package forwarder

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"sync/atomic"
	"time"

	"github.com/stelviodev/traforo/internal/diagnostics"
	"github.com/stelviodev/traforo/internal/dnsrelay"
)

var diagnosticsStarted = time.Now()
var diagnosticsEpoch = func() string { var b [16]byte; _, _ = rand.Read(b[:]); return hex.EncodeToString(b[:]) }()

type tcpMetrics struct{ Active, Opened, Completed, Failed, Refused, QueueDrops atomic.Uint64 }

func (m *tcpMetrics) snapshot() map[string]uint64 {
	return map[string]uint64{"active": m.Active.Load(), "opened": m.Opened.Load(), "completed": m.Completed.Load(), "failed": m.Failed.Load(), "refused": m.Refused.Load(), "queue_drops": m.QueueDrops.Load()}
}

type diagnosticUnit struct {
	id                            uint32
	generation, attempt, revision uint64
	unit                          *unit
	tcp                           *tcpMetrics
	dns                           *dnsrelay.Metrics
	port                          uint16
	claims                        []string
	upstreamEnabled               bool
}

func (m *manager) diagnosticsVersion() int {
	if m.diagnosticsEnabled {
		return 1
	}
	return 0
}

// Called only on the mutation loop after a command. Readers use the projection,
// never the mutation-owned DNS map or a lock held during network operations.
func (m *manager) refreshDiagnostics() {
	m.mu.Lock()
	units := []diagnosticUnit{}
	for id, generation := range m.unitGenerations {
		value := diagnosticUnit{id: id, generation: generation, attempt: m.generations[id], unit: m.units[id], tcp: m.totals[id]}
		if d := m.dns[id]; d != nil {
			value.dns = d.metrics
			value.revision = d.revision
			value.claims = append([]string{}, d.claims...)
			if d.relay != nil {
				value.port = d.relay.Port()
				value.upstreamEnabled = d.relay.UpstreamEnabled()
			}
		}
		units = append(units, value)
	}
	m.mu.Unlock()
	m.diagMu.Lock()
	m.diagUnits = units
	m.diagMu.Unlock()
}
func (m *manager) diagnosticSnapshot() any {
	m.diagMu.Lock()
	units := append([]diagnosticUnit{}, m.diagUnits...)
	m.diagMu.Unlock()
	result := []any{}
	for _, u := range units {
		value := map[string]any{"unit": fmt.Sprintf("%08x", u.id), "generation": u.generation, "attempt": u.attempt, "dns_revision": u.revision, "dns_port": u.port, "dns_upstream_enabled": u.upstreamEnabled, "dns_claims": u.claims, "queue_occupancy": 0, "queue_capacity": packetQueue}
		if u.unit != nil {
			value["queue_occupancy"] = len(u.unit.packets)
			ranges := []string{}
			for _, prefix := range u.unit.cidrs {
				ranges = append(ranges, prefix.String())
			}
			value["ranges"] = ranges
			value["proxy_address"] = u.unit.proxy
		}
		if u.tcp != nil {
			value["tcp"] = u.tcp.snapshot()
		}
		if u.dns != nil {
			value["dns"] = u.dns.Snapshot()
		}
		result = append(result, value)
	}
	return map[string]any{"pid": os.Getpid(), "build_version": diagnostics.BuildVersion, "version": 1, "epoch": diagnosticsEpoch, "sample_monotonic_ns": time.Since(diagnosticsStarted).Nanoseconds(), "units": result}
}
