// Package diagnostics implements the private, read-only snapshot channel.
package diagnostics

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"time"

	"golang.org/x/sys/unix"
)

const Limit = 256 * 1024
const Version = 1

// BuildVersion is set by the executable before either role starts.
var BuildVersion = "dev"

// Request deliberately has no administrative or capability fields.
type Request struct {
	Version       int     `json:"version"`
	Operation     string  `json:"operation"`
	AfterSequence *uint64 `json:"after_sequence"`
}

func Decode(data []byte) error {
	if len(data) == 0 || len(data) > 4096 {
		return errors.New("diagnostics request size")
	}
	// Reject duplicate keys before decoding the closed request schema.
	d := json.NewDecoder(bytes.NewReader(data))
	token, err := d.Token()
	if err != nil || token != json.Delim('{') {
		return errors.New("diagnostics object required")
	}
	seen := map[string]bool{}
	for d.More() {
		key, e := d.Token()
		if e != nil {
			return e
		}
		name, ok := key.(string)
		if !ok || seen[name] {
			return errors.New("duplicate diagnostics key")
		}
		seen[name] = true
		var value json.RawMessage
		if e = d.Decode(&value); e != nil {
			return e
		}
	}
	if _, err = d.Token(); err != nil {
		return err
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return errors.New("trailing diagnostics value")
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	var r Request
	if err = d.Decode(&r); err != nil {
		return err
	}
	if len(seen) != 3 || r.Version != Version || r.Operation != "snapshot" || r.AfterSequence == nil {
		return errors.New("invalid diagnostics request")
	}
	return nil
}

type descriptorReader struct{ connection *net.UnixConn }

func (r descriptorReader) Read(p []byte) (int, error) {
	oob := make([]byte, 4096)
	n, count, flags, _, err := r.connection.ReadMsgUnix(p, oob)
	if count != 0 {
		messages, _ := unix.ParseSocketControlMessage(oob[:count])
		for _, message := range messages {
			descriptors, _ := unix.ParseUnixRights(&message)
			for _, fd := range descriptors {
				_ = unix.Close(fd)
			}
		}
	}
	if count != 0 || flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0 {
		return 0, errors.New("diagnostics descriptors forbidden")
	}
	return n, err
}

// Serve closes only the optional diagnostics connection on any failure.
func Serve(connection net.Conn, snapshot func() any) {
	defer func() { _ = connection.Close() }()
	var reader io.Reader = connection
	if native, ok := connection.(*net.UnixConn); ok {
		reader = descriptorReader{native}
	}
	for {
		// Idle diagnostics must survive between polls. The deadline begins at the first byte.
		_ = connection.SetReadDeadline(time.Time{})
		header := make([]byte, 4)
		if _, err := io.ReadFull(reader, header[:1]); err != nil {
			return
		}
		if connection.SetDeadline(time.Now().Add(500*time.Millisecond)) != nil {
			return
		}
		if _, err := io.ReadFull(reader, header[1:]); err != nil {
			return
		}
		size := binary.BigEndian.Uint32(header)
		if size == 0 || size > 4096 {
			return
		}
		request := make([]byte, int(size))
		if _, err := io.ReadFull(reader, request); err != nil || Decode(request) != nil {
			return
		}
		data, err := json.Marshal(snapshot())
		if err != nil || len(data) > Limit {
			return
		}
		binary.BigEndian.PutUint32(header, uint32(len(data)))
		if _, err = io.Copy(connection, bytes.NewReader(append(header, data...))); err != nil {
			return
		}
	}
}
