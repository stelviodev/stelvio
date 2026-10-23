package dnsrelay

import "sync/atomic"

// Metrics stays bounded and keeps one logical unit's counters across relay replacements.
type Metrics struct {
	UDP, TCP, Active, Overload, Failures, Timeouts, Stale atomic.Uint64
	Exchanges, DurationNS, MaximumNS                      atomic.Uint64
	Responses                                             [16]atomic.Uint64
}

func (m *Metrics) Snapshot() map[string]any {
	codes := make([]uint64, len(m.Responses))
	for i := range codes {
		codes[i] = m.Responses[i].Load()
	}
	return map[string]any{
		"udp_queries":             m.UDP.Load(),
		"tcp_queries":             m.TCP.Load(),
		"active_queries":          m.Active.Load(),
		"overload":                m.Overload.Load(),
		"upstream_failures":       m.Failures.Load(),
		"upstream_timeouts":       m.Timeouts.Load(),
		"stale_response_discards": m.Stale.Load(),
		"upstream_exchanges":      m.Exchanges.Load(),
		"upstream_duration_ns":    m.DurationNS.Load(),
		"upstream_maximum_ns":     m.MaximumNS.Load(),
		"response_codes":          codes,
	}
}

func (m *Metrics) duration(value uint64) {
	m.Exchanges.Add(1)
	m.DurationNS.Add(value)
	for {
		old := m.MaximumNS.Load()
		if old >= value || m.MaximumNS.CompareAndSwap(old, value) {
			return
		}
	}
}

// UpstreamEnabled reports the current generation's gate, not network reachability.
func (r *Relay) UpstreamEnabled() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return !r.closed && r.current != nil && r.current.ctx.Err() == nil
}
