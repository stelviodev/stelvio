package main

import (
	"io"
	"os"
	"testing"
)

func TestVersionOutput(t *testing.T) {
	previousVersion, previousArgs, previousStdout := version, os.Args, os.Stdout
	t.Cleanup(func() {
		version, os.Args, os.Stdout = previousVersion, previousArgs, previousStdout
	})
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = reader.Close() }()
	defer func() { _ = writer.Close() }()
	os.Stdout = writer
	os.Args = []string{"traforo", "version"}
	for _, expected := range []string{"v0.0.5", "v0.0.5-dev+gabcdef123456"} {
		version = expected
		main()
		got := make([]byte, len(expected)+1)
		if _, err := io.ReadFull(reader, got); err != nil {
			t.Fatal(err)
		}
		if string(got) != expected+"\n" {
			t.Fatalf("version output = %q", got)
		}
	}
}
