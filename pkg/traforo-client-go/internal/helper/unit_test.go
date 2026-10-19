//go:build darwin

package helper

import (
	"testing"

	"github.com/stelviodev/traforo/internal/darwin"
)

func TestRouteConflictIn(t *testing.T) {
	const (
		targetNetwork = uint32(0x0afe0000) // 10.254.0.0/16
		targetMask    = uint32(0xffff0000)
	)
	tests := []struct {
		name     string
		routes   []darwin.Route
		index    uint32
		conflict bool
	}{
		{name: "no routes"},
		{name: "nonoverlapping route", routes: []darwin.Route{{Network: 0x0b000000, Mask: 0xff000000, Index: 3}}},
		{name: "default route is not treated as ownership", routes: []darwin.Route{{Network: 0, Mask: 0, Index: 3}}},
		{name: "same interface owns route", routes: []darwin.Route{{Network: 0x0a000000, Mask: 0xff000000, Index: 3}}, index: 3},
		{name: "overlapping foreign route", routes: []darwin.Route{{Network: 0x0a000000, Mask: 0xff000000, Index: 3}}, index: 4, conflict: true},
		{name: "overlapping route with unknown device", routes: []darwin.Route{{Network: targetNetwork, Mask: targetMask, Index: 3}}, conflict: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := routeConflictIn(tc.routes, targetNetwork, targetMask, tc.index)
			if (err != nil) != tc.conflict {
				t.Fatalf("routeConflictIn error = %v, conflict=%v", err, tc.conflict)
			}
		})
	}
}
