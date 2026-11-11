package dnsrelay

import (
	"testing"

	"codeberg.org/miekg/dns"
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
