//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"math"
	"net"
	"net/netip"
	"stelvio.dev/traforo/darwin"
	"stelvio.dev/traforo/protocol"
	"time"
)

func network(p netip.Prefix) (uint32, uint32) {
	a := p.Addr().As4()
	return binary.BigEndian.Uint32(a[:]), uint32(math.MaxUint32) << (32 - p.Bits())
}
func routeStatus(p netip.Prefix, index uint32) (int, error) {
	n, m := network(p)
	routes, e := darwin.Routes()
	if e != nil {
		return -1, e
	}
	status := 0
	for _, r := range routes {
		if r.Mask == m && r.Network == n {
			if status != 0 || index == 0 || r.Index != index {
				return 2, nil
			}
			status = 1
		}
	}
	return status, nil
}
func routeConflict(n, m, index uint32) error {
	routes, e := darwin.Routes()
	if e != nil {
		return e
	}
	for _, r := range routes {
		common := m & r.Mask
		if r.Mask != 0 && n&common == r.Network&common && (index == 0 || r.Index != index) {
			return uncertain
		}
	}
	return nil
}
func live(u Unit, d darwin.Device) bool { return u.Index == d.Index && d.Current() }
func savePhase(store Store, state *Snapshot, i int, phase byte, generation uint64, keep bool) error {
	next := state.Clone()
	next.Revision++
	u := &next.Units[i]
	u.Phase = phase
	u.Generation = generation
	u.KeepDNS = keep
	if e := store.Save(next); e != nil {
		return e
	}
	*state = next
	return nil
}
func finishPrepare(store Store, state *Snapshot, devices *[8]darwin.Device, i int, stage *byte) error {
	u := state.Units[i]
	d := devices[i]
	*stage = 2
	if !live(u, d) {
		return uncertain
	}
	*stage = 3
	if e := resolverConflict(store, *state, u.Request); e != nil {
		return e
	}
	*stage = 4
	local := uint32(0xc0000201 + 2*i)
	if routeConflict(local, math.MaxUint32, d.Index) != nil || routeConflict(local+1, math.MaxUint32, d.Index) != nil {
		return uncertain
	}
	if e := d.Configure(local); e != nil {
		return e
	}
	*stage = 5
	for _, p := range u.Request.Ranges {
		status, e := routeStatus(p, d.Index)
		if e != nil || status == 2 {
			return uncertain
		}
		if status == 0 {
			n, m := network(p)
			if routeConflict(n, m, d.Index) != nil {
				return uncertain
			}
			if e = d.AddRoute(n, m); e != nil {
				return e
			}
		}
		status, e = routeStatus(p, d.Index)
		if e != nil || status != 1 || !d.Current() {
			return uncertain
		}
	}
	for j := range u.Request.Resolvers {
		*stage = 6
		if state.Units[i].Files[j].Phase == 0 {
			if e := stageResolver(store, state, i, j); e != nil {
				return e
			}
		}
		*stage = 7
		if state.Units[i].Files[j].Phase == 1 {
			if e := publishResolver(store, state, i, j); e != nil {
				return e
			}
		}
	}
	*stage = 8
	if e := resolverConflict(store, *state, u.Request); e != nil {
		return e
	}
	*stage = 9
	for j := range u.Request.Resolvers {
		if !resolverCurrent(store, *state, i, j) {
			return uncertain
		}
	}
	*stage = 10
	for _, p := range u.Request.Ranges {
		status, e := routeStatus(p, d.Index)
		if e != nil || status != 1 {
			return uncertain
		}
	}
	*stage = 11
	return savePhase(store, state, i, Active, u.Generation, false)
}
func configureUnit(store Store, state *Snapshot, devices *[8]darwin.Device, packet []byte, stage *byte) error {
	*stage = 1
	r, e := protocol.Decode(packet)
	if e != nil || r.Operation != protocol.Configure || r.Generation == math.MaxUint64 || state.Revision == math.MaxUint64 || r.Session != state.Session || r.Capability != state.Capability || !store.Current(*state) {
		return uncertain
	}
	i := len(state.Units)
	for j, u := range state.Units {
		if u.Request.Unit == r.Unit {
			i = j
			break
		}
	}
	if i < len(state.Units) && state.Units[i].Phase != Removed {
		u := state.Units[i]
		*stage = 12
		if !live(u, devices[i]) {
			return uncertain
		}
		if !bytes.Equal(u.Packet, packet) {
			*stage = 13
			if u.Phase != Active || !protocol.Extension(u.Request, r) || resolverConflict(store, *state, r) != nil {
				return uncertain
			}
			*stage = 14
			for j := range u.Files {
				if !resolverCurrent(store, *state, i, j) {
					return uncertain
				}
			}
			*stage = 21
			next := state.Clone()
			next.Revision++
			next.Units[i].Phase = Preparing
			next.Units[i].Packet = append([]byte(nil), packet...)
			next.Units[i].Request = r
			next.Units[i].Files = append(next.Units[i].Files, make([]Receipt, len(r.Resolvers)-len(u.Files))...)
			*stage = 15
			if e := store.Save(next); e != nil {
				return e
			}
			*state = next
			u = state.Units[i]
		}
		if u.Phase == Preparing {
			return finishPrepare(store, state, devices, i, stage)
		}
		*stage = 16
		if u.Phase != Active || resolverConflict(store, *state, r) != nil {
			return uncertain
		}
		*stage = 17
		for j := range u.Files {
			if !resolverCurrent(store, *state, i, j) {
				return uncertain
			}
		}
		*stage = 18
		for _, p := range r.Ranges {
			status, e := routeStatus(p, devices[i].Index)
			if e != nil || status != 1 {
				return uncertain
			}
		}
		return nil
	}
	*stage = 19
	if i >= 8 || devices[i].FD != -1 || i < len(state.Units) && r.Generation <= state.Units[i].Generation || resolverConflict(store, *state, r) != nil {
		return uncertain
	}
	*stage = 20
	for _, p := range r.Ranges {
		n, m := network(p)
		if e := routeConflict(n, m, 0); e != nil {
			return e
		}
	}
	*stage = 21
	next := state.Clone()
	u := Unit{Phase: Preparing, Generation: r.Generation, Packet: append([]byte(nil), packet...), Request: r, Files: make([]Receipt, len(r.Resolvers))}
	*stage = 22
	d, e := darwin.OpenDevice()
	if e != nil {
		return e
	}
	devices[i] = d
	u.Index = d.Index
	next.Revision++
	if i == len(next.Units) {
		next.Units = append(next.Units, u)
	} else {
		next.Units[i] = u
	}
	*stage = 23
	if e = store.Save(next); e != nil {
		devices[i].Close()
		return e
	}
	*state = next
	return finishPrepare(store, state, devices, i, stage)
}
func detached(u Unit) bool {
	deadline := time.Now().Add(3 * time.Second)
	for {
		_, e := net.InterfaceByIndex(int(u.Index))
		absent := e != nil
		// net.InterfaceByIndex does not expose errno. A complete inventory establishes absence.
		if e != nil {
			interfaces, err := net.Interfaces()
			if err != nil {
				return false
			}
			for _, iface := range interfaces {
				if uint32(iface.Index) == u.Index {
					absent = false
				}
			}
		}
		for _, p := range u.Request.Ranges {
			status, e := routeStatus(p, 0)
			if e != nil || status != 0 {
				absent = false
			}
		}
		if absent {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(10 * time.Millisecond)
	}
}
func removeUnit(store Store, state *Snapshot, devices *[8]darwin.Device, id uint32, generation uint64, keep bool) error {
	if generation == 0 || keep && generation == math.MaxUint64 || !store.Current(*state) {
		return uncertain
	}
	i := -1
	for j, u := range state.Units {
		if u.Request.Unit == id {
			i = j
			break
		}
	}
	if i < 0 {
		return uncertain
	}
	u := state.Units[i]
	if u.Phase == Removed {
		if generation == u.Generation && !keep && devices[i].FD == -1 {
			return nil
		}
		return uncertain
	}
	if u.Phase == Retained && keep && generation == u.Generation {
		if devices[i].FD != -1 {
			return uncertain
		}
		for j := range u.Files {
			if !resolverCurrent(store, *state, i, j) {
				return uncertain
			}
		}
		return nil
	}
	if u.Phase != Removing {
		if generation <= u.Generation {
			return uncertain
		}
		if keep {
			for _, f := range u.Files {
				if f.Phase != 2 {
					return uncertain
				}
			}
		} else if u.Phase == Preparing {
			for j := range u.Files {
				if state.Units[i].Files[j].Phase == 0 {
					if e := stageResolver(store, state, i, j); e != nil {
						return e
					}
				}
			}
		}
		if e := savePhase(store, state, i, Removing, generation, keep); e != nil {
			return e
		}
	} else if u.KeepDNS && !keep && generation > u.Generation {
		if e := savePhase(store, state, i, Removing, generation, false); e != nil {
			return e
		}
	} else if generation != u.Generation || keep != u.KeepDNS {
		return uncertain
	}
	u = state.Units[i]
	if devices[i].FD >= 0 {
		if !live(u, devices[i]) {
			return uncertain
		}
		if e := devices[i].Close(); e != nil {
			return e
		}
	}
	if !detached(u) {
		return uncertain
	}
	for j := range u.Files {
		if keep {
			if !resolverCurrent(store, *state, i, j) {
				return uncertain
			}
		} else if e := removeResolver(store, state, i, j); e != nil {
			return e
		}
	}
	phase := Removed
	if keep {
		phase = Retained
	}
	return savePhase(store, state, i, phase, generation, keep)
}
func cleanup(store Store, state *Snapshot, devices *[8]darwin.Device) error {
	var failure error
	for i := range devices {
		if e := devices[i].Close(); e != nil {
			failure = e
		}
	}
	loaded, exists, e := store.Load()
	if e != nil {
		return e
	}
	*state = loaded
	if !exists {
		return failure
	}
	for _, u := range loaded.Units {
		if u.Phase == Removed {
			continue
		}
		generation := u.Generation
		if u.Phase != Removing || u.KeepDNS {
			if generation == math.MaxUint64 {
				return uncertain
			}
			generation++
		}
		if e := removeUnit(store, state, devices, u.Request.Unit, generation, false); e != nil {
			failure = e
			current, exists, e := store.Load()
			if e != nil || !exists {
				return uncertain
			}
			*state = current
		}
	}
	if failure != nil {
		return failure
	}
	if !store.Current(*state) {
		return uncertain
	}
	if e = store.Remove(); e != nil {
		return e
	}
	*state = Snapshot{}
	return nil
}
