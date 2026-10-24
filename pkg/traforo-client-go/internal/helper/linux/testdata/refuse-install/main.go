//go:build linux

// A native acceptance fixture: validate a real ELF image, then fail installation
// after replacement so the system bootstrap must restore the prior transaction.
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "version" {
		fmt.Println("v0.0.7")
		return
	}
	os.Exit(19)
}
