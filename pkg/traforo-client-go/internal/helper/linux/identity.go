//go:build linux

package helper

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

var errUncertain = errors.New("linux helper ownership is uncertain; retain recovery evidence")

// Owner is a Linux process receipt, never an authority obtained from journal bytes.
type Owner struct {
	UID   uint32 `json:"uid"`
	PID   int32  `json:"pid"`
	Start uint64 `json:"start_ticks"`
	Boot  string `json:"boot_id"`
}

func process(pid int32, uid uint32) (Owner, int32, error) {
	empty := Owner{}
	if pid <= 1 || uid == 0 {
		return empty, 0, errUncertain
	}
	base := fmt.Sprintf("/proc/%d", pid)
	for _, ns := range []string{"user", "pid", "net"} {
		own, e := os.Readlink("/proc/self/ns/" + ns)
		if e != nil {
			return empty, 0, e
		}
		peer, e := os.Readlink(base + "/ns/" + ns)
		if e != nil || peer != own {
			return empty, 0, errUncertain
		}
	}
	status, e := os.ReadFile(base + "/status")
	if e != nil {
		return empty, 0, e
	}
	valid := false
	for _, line := range strings.Split(string(status), "\n") {
		if strings.HasPrefix(line, "Uid:") {
			fields := strings.Fields(line)
			if len(fields) != 5 {
				return empty, 0, errUncertain
			}
			valid = true
			for _, f := range fields[1:] {
				n, err := strconv.ParseUint(f, 10, 32)
				if err != nil || uint32(n) != uid {
					valid = false
				}
			}
		}
	}
	if !valid {
		return empty, 0, errUncertain
	}
	data, e := os.ReadFile(base + "/stat")
	if e != nil {
		return empty, 0, e
	}
	end := strings.LastIndex(string(data), ") ")
	if end < 0 {
		return empty, 0, errUncertain
	}
	fields := strings.Fields(string(data[end+2:]))
	if len(fields) < 20 {
		return empty, 0, errUncertain
	}
	parent, e := strconv.ParseInt(fields[1], 10, 32)
	if e != nil {
		return empty, 0, e
	}
	start, e := strconv.ParseUint(fields[19], 10, 64)
	if e != nil || start == 0 {
		return empty, 0, errUncertain
	}
	boot, e := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if e != nil {
		return empty, 0, e
	}
	return Owner{uid, pid, start, strings.TrimSpace(string(boot))}, int32(parent), nil
}

func peer(fd int) (Owner, error) {
	cred, e := unix.GetsockoptUcred(fd, unix.SOL_SOCKET, unix.SO_PEERCRED)
	if e != nil {
		return Owner{}, e
	}
	owner, _, e := process(cred.Pid, cred.Uid)
	return owner, e
}

func pin(owner Owner) (int, error) {
	fd, e := unix.PidfdOpen(int(owner.PID), 0)
	if e != nil {
		return -1, e
	}
	current, _, e := process(owner.PID, owner.UID)
	if e != nil || current != owner {
		_ = unix.Close(fd)
		return -1, errUncertain
	}
	return fd, nil
}

func alive(owner Owner, fd int) bool {
	if fd < 0 {
		return false
	}
	poll := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
	var n int
	var e error
	for {
		n, e = unix.Poll(poll, 0)
		if e != unix.EINTR {
			break
		}
	}
	if e != nil || n != 0 {
		return false
	}
	current, _, e := process(owner.PID, owner.UID)
	return e == nil && current == owner
}
