# Traforo development on Debian 13

The reference guest is Lima `debian`, Debian 13 ARM64 with systemd-resolved.
The Mac worktree is shared read/write at its existing absolute path. Git commands
run on the Mac because the worktree's common Git directory is not shared.
The previous Lima configuration is saved as `~/.lima/debian/lima.yaml.before-traforo-linux`.

Run commands through an explicit guest shell:

```sh
limactl shell debian -- bash -lc 'uname -a; cat /etc/os-release'
limactl shell debian -- bash /Users/sebst/.codex/worktrees/45bb/stelvio/scripts/debian/provision.sh
limactl shell debian -- bash /Users/sebst/.codex/worktrees/45bb/stelvio/scripts/debian/sync.sh
```

Provisioning installs checksum-verified uv 0.13.5, Go 1.27.3, GoReleaser 2.18.3,
and golangci-lint 2.15.0 under the guest user's home. It installs Python 3.12 with
uv and leaves system Python unchanged. Lima's existing SSH service is retained;
no additional SSH listener is configured. Use a fresh login shell after setup.

The guest-native development directory is `/home/sebst.guest/src/stelvio-linux`.
`sync.sh` preserves its virtual environments and build outputs. Source edits belong
in the shared Mac worktree. Use guest-local files for native permission, installer,
recovery, and OpenSSH tests; VirtioFS is not the acceptance filesystem.

```sh
cd ~/src/stelvio-linux
uv run python pkg/traforo-client-go/scripts/build.py --build-only
# Set STLV_TRAFORO_DEV_BINARY to the absolute path printed by the builder.
uv run stlv tunnel install
uv run pytest tests/integration/test_tunnel_linux.py --integration-linux-privileged -n 0
```

Linux release downloads are not available yet. The explicit developer override is
required. The helper installs at `/usr/local/libexec/stelvio-traforo`, with
`stelvio-traforo.service`, durable state under `/var/lib/stelvio/tunnel`, and
its control socket under `/run/stelvio/tunnel`. Administration uses sudo.
The CLI and forwarder must run as the ordinary user.

Native tests modify only the Debian guest. Run them serially. They use local
SOCKS/DNS fixtures and do not deploy AWS resources. Keep uncertain recovery
journals and test reports until cleanup has been independently checked.

The VM's tunnel applies to Linux applications inside the VM. Mac applications do
not inherit the guest's routes or DNS configuration.


For real OpenSSH interoperability outside the acceptance runner, use a private
`--basetemp` directory under the guest home. OpenSSH rejects test keys beneath
writable ancestors such as `/tmp`; do not relax StrictModes.
