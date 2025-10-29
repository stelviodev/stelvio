# Building Stelvio Binaries

This document explains how to build standalone binaries for the `stlv` CLI tool using PyOxidizer.

## Important Note: PyOxidizer Compatibility

**Current Status**: PyOxidizer 0.24.0 (the latest release) has compatibility issues with Python 3.12 due to the removal of `distutils` in Python 3.12. This project requires Python 3.12+ for its features.

### Workaround Options:

1. **Build PyOxidizer from source** (main branch has Python 3.12 support)
2. **Use alternative tools** like PyInstaller or Nuitka (which have better Python 3.12 support)
3. **Wait for PyOxidizer 0.25.0 release** with Python 3.12 support

The build script and configuration are ready and will work once PyOxidizer with Python 3.12 support is available.

## Prerequisites

- [uv](https://github.com/astral-sh/uv) - Used for Python package management
- [PyOxidizer](https://pyoxidizer.readthedocs.io/) - Will be automatically installed if not present
- Rust toolchain (for cross-compilation to different targets)

## Quick Start

To build binaries for all supported platforms:

```bash
./build-binaries
```

This will create binaries in the `dist/` directory for:
- macOS ARM64 (Apple Silicon)
- macOS AMD64 (Intel)
- Linux ARM64
- Linux AMD64
- Windows AMD64

## Output

Built binaries are placed in the `dist/` directory with the following naming convention:

- `stlv-{version}-{platform}.tar.gz` (macOS and Linux)
- `stlv-{version}-{platform}.zip` (Windows)

A `SHA256SUMS` file is also generated containing checksums for all built binaries.

## Cross-Compilation Notes

### Building on Different Platforms

PyOxidizer supports cross-compilation, but some platform-specific considerations apply:

1. **macOS binaries** - Best built on macOS with Xcode command line tools
2. **Linux binaries** - Can be built on Linux or macOS with appropriate cross-compilation toolchains
3. **Windows binaries** - Require the MSVC toolchain; best built on Windows or with cross-compilation setup

### Setting Up Cross-Compilation

For cross-platform builds, you may need to install additional Rust targets:

```bash
# macOS targets
rustup target add x86_64-apple-darwin
rustup target add aarch64-apple-darwin

# Linux targets
rustup target add x86_64-unknown-linux-gnu
rustup target add aarch64-unknown-linux-gnu

# Windows target
rustup target add x86_64-pc-windows-msvc
```

## PyOxidizer Configuration

The build process uses `pyoxidizer.bzl` for configuration. Key settings:

- **Python version**: 3.12
- **Optimization level**: 2 (removes docstrings and optimizes bytecode)
- **Resources location**: In-memory with filesystem fallback for compatibility
- **Dependencies**: Automatically included from `pyproject.toml`

## Customization

To modify the build:

1. Edit `pyoxidizer.bzl` to change PyOxidizer settings
2. Edit `build-binaries` to modify the build process or add/remove platforms

## Troubleshooting

### Build Failures

If a build fails for a specific platform:

1. Check the build log at `/tmp/pyoxidizer-build.log`
2. Ensure the appropriate Rust target is installed
3. Verify that all dependencies are compatible with the target platform

### Binary Size

PyOxidizer creates standalone binaries that include the Python interpreter and all dependencies. Typical sizes:

- **Compressed**: 20-40 MB per platform
- **Uncompressed**: 50-100 MB per platform

To reduce size, consider:
- Removing unused dependencies from `pyproject.toml`
- Adjusting the PyOxidizer packaging policy

## CI/CD Integration

The `build-binaries` script can be integrated into CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Build binaries
  run: ./build-binaries

- name: Upload artifacts
  uses: actions/upload-artifact@v4
  with:
    name: stlv-binaries
    path: dist/*
```

## License

Binary distributions include all dependencies and their licenses. See individual package licenses for details.
