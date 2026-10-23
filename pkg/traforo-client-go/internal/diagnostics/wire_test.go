package diagnostics

import (
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"testing"
	"time"
)

func TestDecodeSnapshotOnly(t *testing.T) {
	valid := []byte(`{"version":1,"operation":"snapshot","after_sequence":0}`)
	if err := Decode(valid); err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{
		`{"version":1,"operation":"stop","after_sequence":0}`,
		`{"version":1,"version":1,"operation":"snapshot","after_sequence":0}`,
		`{"version":1,"operation":"snapshot","after_sequence":null}`,
		`{"version":1,"operation":"snapshot","after_sequence":-1}`,
		`{"version":1,"operation":"snapshot","after_sequence":0,"capability":"secret"}`,
		`{"version":1,"operation":"snapshot","after_sequence":0} {}`,
	} {
		if Decode([]byte(value)) == nil {
			t.Fatalf("accepted %s", value)
		}
	}
}
func TestServeSnapshotAndReaderFailure(t *testing.T) {
	server, client := net.Pipe()
	done := make(chan struct{})
	go func() {
		Serve(server, func() any { return map[string]any{"version": 1, "epoch": "test", "bytes": 12} })
		close(done)
	}()
	request := []byte(`{"version":1,"operation":"snapshot","after_sequence":0}`)
	header := make([]byte, 4)
	binary.BigEndian.PutUint32(header, uint32(len(request)))
	if _, err := client.Write(append(header, request...)); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(client, header); err != nil {
		t.Fatal(err)
	}
	payload := make([]byte, binary.BigEndian.Uint32(header))
	if _, err := io.ReadFull(client, payload); err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if json.Unmarshal(payload, &value) != nil || value["bytes"] != float64(12) {
		t.Fatal(string(payload))
	}
	_ = client.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("diagnostics failed to stop")
	}
}
func TestStalledReaderClosesDiagnostics(t *testing.T) {
	server, client := net.Pipe()
	defer func() { _ = client.Close() }()
	done := make(chan struct{})
	go func() { Serve(server, func() any { return map[string]int{"value": 1} }); close(done) }()
	request := []byte(`{"version":1,"operation":"snapshot","after_sequence":0}`)
	header := make([]byte, 4)
	binary.BigEndian.PutUint32(header, uint32(len(request)))
	if _, err := client.Write(append(header, request...)); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("stalled reader blocked diagnostics")
	}
}
func FuzzDecode(f *testing.F) {
	f.Add([]byte(`{"version":1,"operation":"snapshot","after_sequence":0}`))
	f.Fuzz(func(t *testing.T, data []byte) { _ = Decode(data) })
}
