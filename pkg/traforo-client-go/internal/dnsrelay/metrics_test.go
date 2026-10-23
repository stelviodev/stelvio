package dnsrelay

import (
	"testing"

	"codeberg.org/miekg/dns"
	"codeberg.org/miekg/dns/dnsutil"
)

func TestMetricsInitializesForZeroValueRelay(t *testing.T) {
	relay := &Relay{}
	if _, err := relay.Answer(rawQuery("outside.test", dns.TypeA), true); err != nil {
		t.Fatal(err)
	}
	if relay.metrics == nil {
		t.Fatal("zero-value relay did not initialize metrics")
	}
	if relay.metrics.UDP.Load() != 1 || relay.metrics.Responses[dns.RcodeRefused].Load() != 1 {
		t.Fatal(relay.metrics.Snapshot())
	}
}

func TestMetricsSurviveRelayReplacement(t *testing.T) {
	metrics := &Metrics{}
	query := rawQuery("outside.test", dns.TypeA)

	first, err := NewWithMetrics(config("127.0.0.1:65534", 1), metrics)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = first.Answer(query, true); err != nil {
		t.Fatal(err)
	}
	first.Close()

	second, err := NewWithMetrics(config("127.0.0.1:65534", 2), metrics)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(second.Close)
	if _, err = second.Answer(query, true); err != nil {
		t.Fatal(err)
	}
	if second.metrics != metrics || metrics.UDP.Load() != 2 || metrics.Responses[dns.RcodeRefused].Load() != 2 {
		t.Fatal(metrics.Snapshot())
	}
}

func TestMetricsRecordRefusedQueriesWithoutQueryHistory(t *testing.T) {
	relay := &Relay{}
	query := new(dns.Msg)
	dnsutil.SetQuestion(query, "outside.example.", dns.TypeA)
	wire, err := packDNS(query)
	if err != nil {
		t.Fatal(err)
	}
	for range 3 {
		if _, err := relay.Answer(wire, true); err != nil {
			t.Fatal(err)
		}
	}
	if relay.metrics.UDP.Load() != 3 || relay.metrics.Responses[dns.RcodeRefused].Load() != 3 || relay.metrics.Active.Load() != 0 {
		t.Fatal(relay.metrics.Snapshot())
	}
	relay.metrics.duration(12)
	relay.metrics.duration(4)
	if relay.metrics.DurationNS.Load() != 16 || relay.metrics.MaximumNS.Load() != 12 || relay.metrics.Exchanges.Load() != 2 {
		t.Fatal(relay.metrics.Snapshot())
	}
}

func TestDiagnosticsUpstreamGateTracksRejectAndActivate(t *testing.T) {
	relay, err := New(config("127.0.0.1:1080", 1))
	if err != nil {
		t.Fatal(err)
	}
	defer relay.Close()
	if !relay.UpstreamEnabled() {
		t.Fatal("configured upstream disabled")
	}
	if err = relay.Reject(1); err != nil {
		t.Fatal(err)
	}
	if relay.UpstreamEnabled() {
		t.Fatal("rejected upstream reported enabled")
	}
	if err = relay.Activate(config("127.0.0.1:1080", 2)); err != nil {
		t.Fatal(err)
	}
	if !relay.UpstreamEnabled() {
		t.Fatal("reactivated upstream disabled")
	}
}
