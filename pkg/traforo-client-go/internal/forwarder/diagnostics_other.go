//go:build !darwin && !linux

package forwarder

import "net"

func inheritedDiagnostics() net.Conn { return nil }
