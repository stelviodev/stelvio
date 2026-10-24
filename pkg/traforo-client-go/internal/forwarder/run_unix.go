//go:build darwin || linux

package forwarder

import (
	"errors"
	"net"
	"os"
	"strconv"

	"golang.org/x/sys/unix"
)

// Run starts the nonroot packet forwarder using the inherited helper carrier.
func Run(carrierFD string) error {
	if !unprivileged(os.Getuid(), os.Geteuid()) {
		return errors.New("nonroot execution required")
	}
	fd, err := strconv.Atoi(carrierFD)
	if err != nil || fd < 3 {
		return errors.New("invalid carrier descriptor")
	}
	file := os.NewFile(uintptr(fd), "carrier")
	if file == nil {
		return errors.New("invalid carrier descriptor")
	}
	carrier, err := carrierFromFile(file)
	if err != nil {
		return err
	}
	return serve(carrier, os.Stdin, os.Stdout)
}

func unprivileged(realUID, effectiveUID int) bool {
	return realUID == effectiveUID && effectiveUID != 0
}

// carrierFromFile consumes file and closes the inherited descriptor after
// duplicating it into a managed net.UnixConn.
func carrierFromFile(file *os.File) (*net.UnixConn, error) {
	raw, err := file.SyscallConn()
	if err != nil {
		_ = file.Close()
		return nil, err
	}
	var socketType int
	var socketErr error
	err = raw.Control(func(fd uintptr) {
		socketType, socketErr = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_TYPE)
		if socketErr == nil && socketType == unix.SOCK_DGRAM {
			var peer unix.Sockaddr
			peer, socketErr = unix.Getpeername(int(fd))
			if _, ok := peer.(*unix.SockaddrUnix); socketErr == nil && !ok {
				socketErr = errors.New("carrier peer is not a Unix socket")
			}
		}
	})
	if err != nil || socketErr != nil || socketType != unix.SOCK_DGRAM {
		_ = file.Close()
		if err != nil {
			return nil, err
		}
		if socketErr != nil {
			return nil, socketErr
		}
		return nil, errors.New("carrier must be a connected Unix datagram socket")
	}
	connection, connErr := net.FileConn(file)
	closeErr := file.Close()
	if connErr != nil {
		return nil, errors.Join(connErr, closeErr)
	}
	if closeErr != nil {
		_ = connection.Close()
		return nil, closeErr
	}
	carrier, ok := connection.(*net.UnixConn)
	if !ok {
		_ = connection.Close()
		return nil, errors.New("carrier must be a Unix socket")
	}
	return carrier, nil
}
