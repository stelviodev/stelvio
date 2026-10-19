// Traforo keeps its privileged and nonprivileged roles in distinct processes.
package main

import (
	"fmt"
	"io"
	"os"

	"github.com/stelviodev/traforo/internal/forwarder"
	"github.com/stelviodev/traforo/internal/helper"
)

var (
	version = "dev"
	commit  = "unknown"
)

func execute(args []string, stdout, stderr io.Writer) int {
	return executeWithForwarder(args, stdout, stderr, forwarder.Run)
}

func executeWithForwarder(args []string, stdout, stderr io.Writer, runForwarder func(string) error) int {
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
			_, _ = fmt.Fprintln(stderr, "Traforo operation failed:", err)
			return 1
		}
		return 0
	case len(args) == 2 && args[0] == "forwarder":
		if err := runForwarder(args[1]); err != nil {
			_, _ = fmt.Fprintln(stderr, "Traforo operation failed:", err)
			return 1
		}
		return 0
	default:
		_, _ = fmt.Fprintln(stderr, "usage: stelvio-traforo version | helper serve/install/uninstall | forwarder <carrier-fd>")
		return 2
	}
}

func main() {
	os.Exit(execute(os.Args[1:], os.Stdout, os.Stderr))
}
