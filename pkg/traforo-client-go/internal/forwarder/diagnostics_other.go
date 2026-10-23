//go:build !darwin

package forwarder

import "net"

func inheritedDiagnostics() net.Conn { return nil }
