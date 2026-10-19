//go:build darwin

// Traforo keeps its privileged and nonprivileged roles in distinct processes.
package main

import (
	"fmt"
	"io"
	"os"

	"github.com/stelviodev/traforo/internal/helper"
)

var (
	version = "dev"
	commit  = "unknown"
)

func execute(args []string, stdout, stderr io.Writer) int {
	switch {
	case len(args) == 1 && args[0] == "version":
		if version == "dev" {
			_, _ = fmt.Fprintln(stdout, commit)
		} else {
			_, _ = fmt.Fprintln(stdout, version)
		}
		return 0
	case len(args) == 2 && args[0] == "helper":
		if err := helper.Run(args[1]); err != nil {
			_, _ = fmt.Fprintln(stderr, "Traforo operation failed; ownership evidence retained:", err)
			return 1
		}
		return 0
	default:
		_, _ = fmt.Fprintln(stderr, "usage: stelvio-traforo version | helper serve/install/uninstall")
		return 2
	}
}

func main() {
	os.Exit(execute(os.Args[1:], os.Stdout, os.Stderr))
}
