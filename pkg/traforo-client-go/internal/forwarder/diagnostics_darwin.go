//go:build darwin

package forwarder

import (
	"net"
	"os"
	"strconv"

	"golang.org/x/sys/unix"
)

func inheritedDiagnostics() net.Conn {
	text := os.Getenv("STLV_TRAFORO_DIAGNOSTICS_FD")
	if text == "" {
		return nil
	}
	fd, err := strconv.Atoi(text)
	if err != nil || fd < 3 {
		return nil
	}
	file := os.NewFile(uintptr(fd), "diagnostics")
	if file == nil {
		return nil
	}
	defer func() { _ = file.Close() }()
	socketType, err := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_TYPE)
	if err != nil || socketType != unix.SOCK_STREAM {
		return nil
	}
	peer, err := unix.Getpeername(fd)
	if err != nil {
		return nil
	}
	if _, ok := peer.(*unix.SockaddrUnix); !ok {
		return nil
	}
	connection, err := net.FileConn(file)
	if err != nil {
		return nil
	}
	return connection
}
