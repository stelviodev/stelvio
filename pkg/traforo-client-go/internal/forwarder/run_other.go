//go:build !darwin && !linux

package forwarder

import "errors"

var errUnsupportedPlatform = errors.New("forwarder is only supported on Darwin")

// Run rejects host networking outside the supported Darwin runtime.
func Run(string) error { return errUnsupportedPlatform }
