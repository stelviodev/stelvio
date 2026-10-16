// Traforo keeps its privileged and nonprivileged roles in distinct processes.
package main

import (
	"fmt"
	"github.com/stelviodev/traforo/forwarder"
	"github.com/stelviodev/traforo/helper"
	"os"
)

func main() {
	args := os.Args[1:]
	var err error
	switch {
	case len(args) == 1 && args[0] == "version":
		fmt.Println("stelvio-traforo/1 helper=1 carrier=1")
		return
	case len(args) == 2 && args[0] == "helper":
		err = helper.Run(args[1])
	case len(args) == 2 && args[0] == "forwarder":
		err = forwarder.Run(append([]string{"forwarder"}, args[1:]...))
	default:
		fmt.Fprintln(os.Stderr, "usage: stelvio-traforo version | helper serve/install/uninstall | forwarder <carrier-fd>")
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "Traforo operation failed; ownership evidence retained:", err)
		os.Exit(1)
	}
}
