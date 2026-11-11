//go:build darwin

package helper

import darwinhelper "github.com/stelviodev/traforo/internal/helper/darwin"

// Run dispatches a privileged helper operation to the Darwin implementation.
func Run(operation string) error {
	return darwinhelper.Run(operation)
}
