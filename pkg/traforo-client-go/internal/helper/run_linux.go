//go:build linux

package helper

import linuxhelper "github.com/stelviodev/traforo/internal/helper/linux"

// Run dispatches privileged Linux operations without initialization side effects.
func Run(operation string) error { return linuxhelper.Run(operation) }
