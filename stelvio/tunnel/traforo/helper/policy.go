package helper

import (
	"bytes"
	"math"
	"stelvio.dev/traforo/protocol"
)

// Authorized compares immutable actor receipts. The native backend must obtain
// and validate the live peer before calling this policy; journal bytes are not
// evidence of a live process or a transferable privilege.
func authorized(state Snapshot, peer protocol.DarwinOwner, r protocol.Request) bool {
	return peer == state.Peer && r.Session == state.Session && r.Capability == state.Capability
}

func validChange(state Snapshot, packet []byte, r protocol.Request) bool {
	var u *Unit
	for i := range state.Units {
		if state.Units[i].Request.Unit == r.Unit {
			u = &state.Units[i]
		}
	}
	if r.Operation == protocol.Configure {
		if r.Generation == math.MaxUint64 {
			return false
		}
		if u == nil {
			return true
		}
		if u.Phase == Removed {
			return r.Generation > u.Generation
		}
		if u.Phase == Active && protocol.Extension(u.Request, r) {
			return true
		}
		return (u.Phase == Active || u.Phase == Preparing) && bytes.Equal(u.Packet, packet)
	}
	if r.Operation != protocol.Remove {
		return true
	}
	if u == nil || r.KeepDNS && r.Generation == math.MaxUint64 {
		return false
	}
	if u.Phase == Removed {
		return !r.KeepDNS && r.Generation == u.Generation
	}
	if u.Phase == Retained && r.KeepDNS {
		return r.Generation == u.Generation
	}
	if u.Phase == Removing {
		return r.Generation == u.Generation && r.KeepDNS == u.KeepDNS || u.KeepDNS && !r.KeepDNS && r.Generation > u.Generation
	}
	return r.Generation > u.Generation
}
