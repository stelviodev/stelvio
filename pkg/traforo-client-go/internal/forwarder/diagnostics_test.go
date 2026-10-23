package forwarder

import (
	"sync"
	"testing"
)

func TestDiagnosticCountersRemainIndependentOfMutations(t *testing.T) {
	metrics := &tcpMetrics{}
	m := &manager{unitGenerations: map[uint32]uint64{1: 3}, generations: map[uint32]uint64{1: 2}, totals: map[uint32]*tcpMetrics{1: metrics}}
	m.refreshDiagnostics()
	var wait sync.WaitGroup
	for range 4 {
		wait.Go(func() {
			for range 1000 {
				metrics.Opened.Add(1)
				_ = m.diagnosticSnapshot()
			}
		})
	}
	wait.Wait()
	if metrics.Opened.Load() != 4000 {
		t.Fatal("lost increments")
	}
	// Retiring a stack does not discard the logical unit counter identity.
	m.generations[1] = 4
	m.unitGenerations[1] = 5
	m.refreshDiagnostics()
	if m.totals[1] != metrics || metrics.Opened.Load() != 4000 {
		t.Fatal("lost reconnect totals")
	}
}
