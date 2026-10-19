//go:build darwin

package helper

import (
	"os"
	"reflect"
	"testing"

	"golang.org/x/sys/unix"
)

func setReceipt(t *testing.T, file *os.File, receipt installationReceipt) {
	t.Helper()
	data, err := receipt.encode()
	if err != nil {
		t.Fatal(err)
	}
	if err = unix.Fsetxattr(int(file.Fd()), installationAttribute, data, 0); err != nil {
		t.Fatal(err)
	}
}

func TestInstallationReceiptRoundTripAndRecoveryStages(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "installation-receipt-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = file.Close() })

	st, err := stat(int(file.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	image := identityOf(st)

	if _, present, err := readInstallation(int(file.Fd())); err != nil || present {
		t.Fatalf("missing receipt = present %v, error %v", present, err)
	}

	for _, stage := range []string{"", ".tunnel.A7c91fE0", ".tunnel.retired"} {
		t.Run(stage, func(t *testing.T) {
			want := installationReceipt{
				Image:     image,
				Directory: identity{Device: 12, Inode: 34},
				Daemon:    identity{Device: 56, Inode: 78},
				Socket:    identity{Device: 90, Inode: 123},
				Staging:   stage,
			}
			setReceipt(t, file, want)
			got, present, err := readInstallation(int(file.Fd()))
			if err != nil || !present || !reflect.DeepEqual(got, want) {
				t.Fatalf("receipt = %#v, present=%v, error=%v; want %#v", got, present, err, want)
			}
		})
	}
}

func TestInstallationReceiptRejectsInvalidOwnershipAndStaging(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "installation-receipt-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = file.Close() })
	st, err := stat(int(file.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	valid := installationReceipt{Image: identityOf(st), Staging: ".tunnel.valid123"}

	t.Run("wrong image", func(t *testing.T) {
		wrong := valid
		wrong.Image.Inode++
		setReceipt(t, file, wrong)
		if _, present, err := readInstallation(int(file.Fd())); err == nil || present {
			t.Fatalf("accepted mismatched image receipt: present=%v error=%v", present, err)
		}
	})

	t.Run("invalid staging path", func(t *testing.T) {
		data, err := valid.encode()
		if err != nil {
			t.Fatal(err)
		}
		copy(data[72:136], ".tunnel../foreign")
		if err = unix.Fsetxattr(int(file.Fd()), installationAttribute, data, 0); err != nil {
			t.Fatal(err)
		}
		if _, present, err := readInstallation(int(file.Fd())); err == nil || present {
			t.Fatalf("accepted invalid staging path: present=%v error=%v", present, err)
		}
	})

	t.Run("corrupt marker", func(t *testing.T) {
		data, err := valid.encode()
		if err != nil {
			t.Fatal(err)
		}
		copy(data, "FOREIGN1")
		if err = unix.Fsetxattr(int(file.Fd()), installationAttribute, data, 0); err != nil {
			t.Fatal(err)
		}
		if _, present, err := readInstallation(int(file.Fd())); err == nil || present {
			t.Fatalf("accepted corrupt marker: present=%v error=%v", present, err)
		}
	})

	t.Run("truncated receipt", func(t *testing.T) {
		data, err := valid.encode()
		if err != nil {
			t.Fatal(err)
		}
		if err = unix.Fsetxattr(int(file.Fd()), installationAttribute, data[:len(data)-1], 0); err != nil {
			t.Fatal(err)
		}
		if _, present, err := readInstallation(int(file.Fd())); err == nil || present {
			t.Fatalf("accepted truncated receipt: present=%v error=%v", present, err)
		}
	})
}

func TestInstallationReceiptRejectsInvalidEncoding(t *testing.T) {
	valid := installationReceipt{
		Image:     identity{Device: 1, Inode: 2},
		Directory: identity{Device: 3, Inode: 4},
	}
	encoded, err := valid.encode()
	if err != nil {
		t.Fatal(err)
	}
	if got, err := decodeInstallation(encoded); err != nil || !reflect.DeepEqual(got, valid) {
		t.Fatalf("decoded receipt = %#v, error=%v; want %#v", got, err, valid)
	}

	trailing := append([]byte(nil), encoded...)
	trailing[len(trailing)-1] = 1
	if _, err := decodeInstallation(trailing); err == nil {
		t.Fatal("accepted nonzero receipt padding")
	}
	partialIdentity := append([]byte(nil), encoded...)
	clear(partialIdentity[32:40])
	if _, err := decodeInstallation(partialIdentity); err == nil {
		t.Fatal("accepted partial directory identity")
	}

	for _, invalid := range []installationReceipt{
		{},
		{Image: identity{Device: 1, Inode: 2}, Directory: identity{Device: 3}},
		{Image: identity{Device: 1, Inode: 2}, Staging: ".tunnel."},
		{Image: identity{Device: 1, Inode: 2}, Staging: ".tunnel.bad/name"},
	} {
		if _, err := invalid.encode(); err == nil {
			t.Fatalf("encoded invalid receipt %#v", invalid)
		}
	}
}

func TestStagingDirectoryRecoveryDecisions(t *testing.T) {
	for _, tc := range []struct {
		name                       string
		sourcePresent, sourceOwned bool
		targetPresent, targetOwned bool
		want                       adminRecoveryAction
	}{
		{name: "staged directory before rename", sourcePresent: true, sourceOwned: true, want: adminRecoveryMove},
		{name: "directory already published", targetPresent: true, targetOwned: true, want: adminRecoveryComplete},
		{name: "foreign staging directory", sourcePresent: true, targetPresent: false, want: adminRecoveryUncertain},
		{name: "foreign published directory", targetPresent: true, want: adminRecoveryUncertain},
		{name: "both paths exist", sourcePresent: true, sourceOwned: true, targetPresent: true, targetOwned: true, want: adminRecoveryUncertain},
		{name: "both paths absent", want: adminRecoveryUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := stagingRecovery(tc.sourcePresent, tc.sourceOwned, tc.targetPresent, tc.targetOwned); got != tc.want {
				t.Fatalf("stagingRecovery = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestDaemonPublicationRecoveryDecisions(t *testing.T) {
	for _, tc := range []struct {
		name                         string
		pendingPresent, pendingOwned bool
		publicPresent, publicOwned   bool
		want                         adminRecoveryAction
	}{
		{name: "pending before publication", pendingPresent: true, pendingOwned: true, want: adminRecoveryMove},
		{name: "publication linked before pending removed", pendingPresent: true, pendingOwned: true, publicPresent: true, publicOwned: true, want: adminRecoveryRemove},
		{name: "publication complete", publicPresent: true, publicOwned: true, want: adminRecoveryComplete},
		{name: "foreign pending daemon", pendingPresent: true, publicPresent: true, publicOwned: true, want: adminRecoveryUncertain},
		{name: "foreign published daemon", pendingPresent: true, pendingOwned: true, publicPresent: true, want: adminRecoveryUncertain},
		{name: "both publication paths absent", want: adminRecoveryUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := daemonRecovery(tc.pendingPresent, tc.pendingOwned, tc.publicPresent, tc.publicOwned); got != tc.want {
				t.Fatalf("daemonRecovery = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestRetiredDirectoryRecoveryDecisions(t *testing.T) {
	for _, tc := range []struct {
		name                                       string
		statePresent, stateOwned                   bool
		retiredPresent, retiredOwned, retiredEmpty bool
		want                                       adminRecoveryAction
	}{
		{name: "terminal intent before move", statePresent: true, stateOwned: true, want: adminRecoveryMove},
		{name: "interrupted after move", retiredPresent: true, retiredOwned: true, retiredEmpty: true, want: adminRecoveryRemove},
		{name: "interrupted after remove", want: adminRecoveryComplete},
		{name: "state directory is foreign", statePresent: true, want: adminRecoveryUncertain},
		{name: "retired directory is foreign", retiredPresent: true, retiredEmpty: true, want: adminRecoveryUncertain},
		{name: "retired directory is not empty", retiredPresent: true, retiredOwned: true, want: adminRecoveryUncertain},
		{name: "both paths exist", statePresent: true, stateOwned: true, retiredPresent: true, retiredOwned: true, retiredEmpty: true, want: adminRecoveryUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := retiredDirectoryRecovery(tc.statePresent, tc.stateOwned, tc.retiredPresent, tc.retiredOwned, tc.retiredEmpty)
			if got != tc.want {
				t.Fatalf("retiredDirectoryRecovery = %v, want %v", got, tc.want)
			}
		})
	}
}
