//go:build !darwin

package helper

import (
	"errors"
	"fmt"
)

var errUnsupportedPlatform = errors.New("helper operations are only supported on Darwin")

// Run rejects host operations on platforms without the Darwin helper.
func Run(operation string) error {
	switch operation {
	case "serve", "install", "uninstall":
		return errUnsupportedPlatform
	default:
		return fmt.Errorf("unknown helper operation")
	}
}
