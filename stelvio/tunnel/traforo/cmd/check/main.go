//go:build darwin

// Independent Python golden vectors exercise the Go implementation without root.
package main

import (
	"encoding/binary"
	"fmt"
	"golang.org/x/sys/unix"
	"io"
	"os"
	"reflect"
	"stelvio.dev/traforo/darwin"
	"stelvio.dev/traforo/helper"
	"stelvio.dev/traforo/protocol"
	"strconv"
)

func run() int {
	args := os.Args[1:]
	if len(args) < 1 {
		return 2
	}
	data, e := io.ReadAll(io.LimitReader(os.Stdin, 2*protocol.MaxJournal+9))
	if e != nil {
		return 2
	}
	switch args[0] {
	case "request":
		return helper.FixtureRequest(data)
	case "peer":
		if len(args) != 2 {
			return 2
		}
		return helper.FixturePeer(args[1])
	case "io":
		if len(args) < 2 || len(args) > 3 {
			return 2
		}
		fd, e := strconv.Atoi(args[1])
		if e != nil {
			return 2
		}
		mode := ""
		if len(args) == 3 {
			mode = args[2]
		}
		return helper.FixtureIO(fd, mode)
	case "snapshot":
		if len(args) == 2 && args[1] == "--successor" {
			if len(data) < 8 {
				return 1
			}
			a, b := int(binary.BigEndian.Uint32(data)), int(binary.BigEndian.Uint32(data[4:]))
			if a > protocol.MaxJournal || b > protocol.MaxJournal || a+b+8 != len(data) {
				return 1
			}
			before, e := helper.DecodeSnapshot(data[8 : 8+a])
			if e != nil {
				return 1
			}
			after, e := helper.DecodeSnapshot(data[8+a:])
			if e != nil || !helper.Successor(before, after) {
				return 1
			}
			return 0
		}
		state, e := helper.DecodeSnapshot(data)
		if e != nil {
			if !reflect.DeepEqual(state, helper.Snapshot{}) {
				return 2
			}
			return 1
		}
		if len(args) == 4 && args[1] == "--resolver" {
			i, e := strconv.Atoi(args[2])
			if e != nil {
				return 2
			}
			j, e := strconv.Atoi(args[3])
			if e != nil {
				return 2
			}
			name, contents, e := helper.ResolverSpec(state, i, j)
			if e != nil {
				return 1
			}
			fmt.Println(name)
			os.Stdout.Write(contents)
			return 0
		}
		if len(args) != 1 {
			return 2
		}
		out, e := helper.EncodeSnapshot(state)
		if e != nil {
			return 1
		}
		os.Stdout.Write(out)
		return 0
	case "journal":
		if len(args) != 1 || len(data) < 8 || len(data) > 24 {
			return 2
		}
		if helper.JournalIncomplete(data[8:], binary.BigEndian.Uint64(data)) {
			return 0
		}
		return 1
	case "packet":
		if len(data) < 9 {
			return 1
		}
		n := int(binary.BigEndian.Uint32(data))
		if n > protocol.Header+protocol.MaxBody || n > len(data)-9 || data[8] > 1 {
			return 1
		}
		r, e := protocol.Decode(data[9 : 9+n])
		packet := data[9+n:]
		if e != nil || len(packet) < 4 || binary.BigEndian.Uint32(packet) != protocol.IPv4 {
			return 1
		}
		if protocol.PacketAllowed(packet[4:], r, binary.BigEndian.Uint32(data[4:]), data[8] == 1) {
			return 0
		}
		return 1
	case "resolver":
		if len(args) == 4 && args[1] == "--overlap" {
			a, e := protocol.Domain(args[2])
			if e != nil {
				return 2
			}
			b, e := protocol.Domain(args[3])
			if e != nil {
				return 2
			}
			if protocol.DNSOverlap(a, b) {
				return 0
			}
			return 1
		}
		if len(args) == 2 && args[1] == "--system" {
			r, e := protocol.Decode(data)
			if e != nil {
				return 2
			}
			domains, e := darwin.DNSDomains()
			if e != nil {
				return 2
			}
			for _, d := range domains {
				d, e = protocol.Domain(d)
				if e != nil {
					return 2
				}
				for _, r := range r.Resolvers {
					if protocol.DNSOverlap(d, r.Domain) {
						return 1
					}
				}
			}
			return 0
		}
		if len(args) != 2 {
			return 2
		}
		d, e := helper.ResolverDomain(args[1], data)
		if e != nil {
			return 1
		}
		fmt.Println(d)
		return 0
	case "acl":
		if len(args) != 2 {
			return 2
		}
		fd, e := unix.Open(args[1], unix.O_EVTONLY|unix.O_NOFOLLOW, 0)
		if e != nil {
			return 2
		}
		defer unix.Close(fd)
		v := 0
		if darwin.NoACL(fd) {
			v = 1
		}
		pathValue := 0
		if darwin.NoACLPath(args[1]) {
			pathValue = 1
		}
		fmt.Printf("%d %d\n", v, pathValue)
		return 0
	}
	return 2
}
func main() { os.Exit(run()) }
