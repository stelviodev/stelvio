# Quick Start: Building Stelvio Binaries

## 1. Run the Build Script

```bash
./build-binaries
```

This will:
- Install PyOxidizer if not present (using uv)
- Build binaries for all supported platforms
- Create compressed archives in `dist/`
- Generate SHA256 checksums

## 2. Supported Platforms

The script builds for:
- **macOS ARM64** (Apple Silicon) - `stlv-{version}-macos-arm64.tar.gz`
- **macOS AMD64** (Intel) - `stlv-{version}-macos-amd64.tar.gz`
- **Linux ARM64** - `stlv-{version}-linux-arm64.tar.gz`
- **Linux AMD64** - `stlv-{version}-linux-amd64.tar.gz`
- **Windows AMD64** - `stlv-{version}-windows-amd64.zip`

## 3. Installing a Built Binary

### macOS/Linux:
```bash
# Extract
tar -xzf stlv-{version}-{platform}.tar.gz

# Move to PATH
sudo mv stlv /usr/local/bin/

# Verify
stlv version
```

### Windows:
```powershell
# Extract the zip file
# Move stlv.exe to a directory in your PATH
# Verify
stlv version
```

## 4. Cross-Compilation Setup

If building on a different platform than the target, install Rust targets:

```bash
# Install all targets
rustup target add x86_64-apple-darwin      # macOS Intel
rustup target add aarch64-apple-darwin     # macOS ARM
rustup target add x86_64-unknown-linux-gnu # Linux AMD64
rustup target add aarch64-unknown-linux-gnu # Linux ARM64
rustup target add x86_64-pc-windows-msvc   # Windows
```

## 5. Troubleshooting

**Build fails for a platform:**
- Check `/tmp/pyoxidizer-build.log`
- Ensure Rust target is installed
- Some cross-compilation may require additional system tools

**Binary too large:**
- Review dependencies in `pyproject.toml`
- Adjust PyOxidizer settings in `pyoxidizer.bzl`

**Binary doesn't run:**
- Verify Python 3.12 compatibility
- Check system dependencies (glibc version on Linux, etc.)

## 6. CI/CD

For automated builds, ensure:
- Rust toolchain is installed
- uv is available
- Appropriate Rust targets are installed

Example GitHub Actions:
```yaml
- name: Install Rust
  uses: actions-rs/toolchain@v1
  with:
    toolchain: stable

- name: Install uv
  run: curl -LsSf https://astral.sh/uv/install.sh | sh

- name: Build binaries
  run: ./build-binaries
```

## More Information

See [BUILD.md](BUILD.md) for detailed documentation.
