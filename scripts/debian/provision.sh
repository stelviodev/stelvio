#!/usr/bin/env bash
# Provision the Debian 13 Traforo development guest. Run as the ordinary user.
set -euo pipefail
umask 022
[ "$(id -u)" != 0 ] || { echo 'Run as the ordinary development user' >&2; exit 1; }
. /etc/os-release
[ "$ID:$VERSION_ID" = debian:13 ] || { echo 'Debian 13 required' >&2; exit 1; }
sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y git build-essential pkg-config ca-certificates curl rsync unzip xz-utils binutils file jq acl openssh-client iproute2 tcpdump dnsutils dbus python3
# Lima already uses openssh-server. Keep its existing service configuration.
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/traforo-tools" "$HOME/src/stelvio-linux"
mkdir -p "$HOME/.config/stelvio/bin"
chmod go-w "$HOME" "$HOME/src" "$HOME/.config" "$HOME/.config/stelvio" "$HOME/.config/stelvio/bin"
export PATH="$HOME/.local/bin:$PATH"
python3 - <<'PY'
import hashlib, io, json, os, pathlib, platform, shutil, tarfile, urllib.request
root = pathlib.Path.home() / '.local/share/traforo-tools'
bin_dir = pathlib.Path.home() / '.local/bin'
arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}[platform.machine()]
uvarch = {'arm64': 'aarch64', 'amd64': 'x86_64'}[arch]
def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()
def install(name, version, url, checksum, executable):
    target = root / f'{name}-{version}'
    if not (target / '.verified').exists():
        data = fetch(url)
        if hashlib.sha256(data).hexdigest() != checksum:
            raise RuntimeError(f'{name}: checksum mismatch')
        target.mkdir(exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            archive.extractall(target, filter='data')
        (target / '.verified').write_text(checksum + '\n')
    source = target / executable
    source.chmod(0o755)
    link = bin_dir / name
    temporary = bin_dir / f'.{name}.new'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source)
    temporary.replace(link)
def release(name, version, repo, filename, executable):
    base = f'https://github.com/{repo}/releases/download/v{version}'
    checks_name = {'uv':'sha256.sum', 'goreleaser':'checksums.txt', 'golangci-lint':f'golangci-lint-{version}-checksums.txt'}[name]
    checks = fetch(f'{base}/{checks_name}').decode().splitlines()
    checksum = next(line.split()[0] for line in checks if line.split()[-1].lstrip('*') == filename)
    install(name, version, f'{base}/{filename}', checksum, executable)
# uv tags have no leading v.
uvfile = f'uv-{uvarch}-unknown-linux-gnu.tar.gz'
uvbase = 'https://github.com/astral-sh/uv/releases/download/0.13.5'
uvchecksum = fetch(f'{uvbase}/{uvfile}.sha256').decode().split()[0]
install('uv', '0.13.5', f'{uvbase}/{uvfile}', uvchecksum, f'uv-{uvarch}-unknown-linux-gnu/uv')
releases = json.loads(fetch('https://go.dev/dl/?mode=json&include=all'))
go = next(f for release in releases if release['version'] == 'go1.27.3' for f in release['files'] if f['os'] == 'linux' and f['arch'] == arch and f['kind'] == 'archive')
install('go', '1.27.3', f'https://go.dev/dl/{go["filename"]}', go['sha256'], 'go/bin/go')
gofmt = bin_dir / 'gofmt'
gofmt.unlink(missing_ok=True)
gofmt.symlink_to(root / 'go-1.27.3/go/bin/gofmt')
release('goreleaser', '2.18.3', 'goreleaser/goreleaser', f'goreleaser_Linux_{"x86_64" if arch == "amd64" else arch}.tar.gz', 'goreleaser')
release('golangci-lint', '2.15.0', 'golangci/golangci-lint', f'golangci-lint-2.15.0-linux-{arch}.tar.gz', f'golangci-lint-2.15.0-linux-{arch}/golangci-lint')
PY
if ! grep -Fq '# Traforo development tools' "$HOME/.profile"; then
    cat >> "$HOME/.profile" <<'PROFILE'

# Traforo development tools
umask 022
export PATH="$HOME/.local/bin:$PATH"
PROFILE
fi
uv python install 3.12
{ uv --version; go version; goreleaser --version; golangci-lint --version; python3 --version; } | tee "$HOME/.local/share/traforo-tools/versions.txt"
