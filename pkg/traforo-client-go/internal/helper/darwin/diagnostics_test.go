//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/stelviodev/traforo/internal/protocol"
	"golang.org/x/sys/unix"
)

func TestDiagnosticsProjectionDoesNotExposeCapabilities(t *testing.T) {
	s := serviceFixture()
	s.epoch = "epoch"
	s.started = time.Now()
	s.metrics = make(map[uint32]*packetMetrics)
	s.grants[0] = grant{phase: Active, generation: 7, request: s.state.Units[0].Request}
	s.packetMetrics(s.grants[0].request.Unit).HostBytesDelivered = 123
	data, err := json.Marshal(s.diagnostics(s.peer.UID))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte("capability")) || !bytes.Contains(data, []byte(`"host_delivered_bytes":123`)) {
		t.Fatal(string(data))
	}
	other, err := json.Marshal(s.diagnostics(s.peer.UID + 1))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(other, []byte("units")) || bytes.Contains(other, []byte("session_id")) {
		t.Fatal(string(other))
	}
}
func TestDiagnosticsAllowsOtherProcessDuringMutation(t *testing.T) {
	s := serviceFixture()
	s.working = true
	s.epoch = "epoch"
	s.started = time.Now()
	pair := servicePair(t, unix.SOCK_STREAM)
	c := client{fd: pair[0], peer: s.peer}
	c.peer.PID++
	s.handle(&c, protocol.Request{Operation: protocol.Diagnostics})
	buffer := make([]byte, 4096)
	n, _, _, _, err := unix.Recvmsg(pair[1], buffer, nil, 0)
	if err != nil || n <= 16 || binary.BigEndian.Uint16(buffer[8:]) != OK {
		t.Fatalf("n=%d err=%v", n, err)
	}
	if !strings.Contains(string(buffer[:n]), `"mutating":true`) {
		t.Fatal(string(buffer[:n]))
	}
	if !s.working {
		t.Fatal("diagnostics changed mutation state")
	}
}
func TestDiagnosticReplyDoesNotExpandLegacyReplyLimit(t *testing.T) {
	pair := servicePair(t, unix.SOCK_STREAM)
	if reply(pair[0], OK, make([]byte, 257), -1) {
		t.Fatal("legacy limit changed")
	}
}
