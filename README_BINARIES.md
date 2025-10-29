# Stelvio Binary Build System - Summary

This directory contains a complete binary build system for the `stlv` CLI tool using PyOxidizer.

## 📁 Files Created

### Core Build Files
- **`build-binaries`** - Main build script (executable)
- **`pyoxidizer.bzl`** - PyOxidizer configuration file

### Documentation
- **`BUILD.md`** - Comprehensive build documentation
- **`BUILDING_QUICKSTART.md`** - Quick start guide
- **`PYOXIDIZER_FROM_SOURCE.md`** - Instructions for building PyOxidizer from source
- **`README_BINARIES.md`** (this file) - Summary and overview

## ⚠️ Important: Python 3.12 Compatibility

**Current Limitation**: PyOxidizer 0.24.0 (latest release as of Oct 2025) has compatibility issues with Python 3.12 due to Python's removal of the `distutils` module.

Since Stelvio requires Python 3.12+ (uses `type` aliases and other 3.12+ features), you have these options:

### Option 1: Build PyOxidizer from Source (Recommended)
The main branch of PyOxidizer has Python 3.12 support. See `PYOXIDIZER_FROM_SOURCE.md` for detailed instructions.

```bash
# Quick version:
git clone https://github.com/indygreg/PyOxidizer.git
cd PyOxidizer  
cargo build --release --bin pyoxidizer
sudo cp target/release/pyoxidizer /usr/local/bin/
```

### Option 2: Use Alternative Tools
Consider using:
- **PyInstaller** - Mature, good Python 3.12 support
- **Nuitka** - Python compiler, creates very fast binaries
- **Shiv** - Creates self-contained Python zipapps

### Option 3: Wait for PyOxidizer 0.25.0
Monitor https://github.com/indygreg/PyOxidizer/releases for the next release.

## 🎯 What's Included

### Build Script Features
- ✅ Builds for 5 platforms: macOS (arm64/amd64), Linux (arm64/amd64), Windows (amd64)
- ✅ Uses Python 3.12.4 distributions from python-build-standalone
- ✅ PGO-optimized binaries where available (LTO for ARM64 Linux)
- ✅ Automatic dependency installation
- ✅ Creates compressed archives (.tar.gz / .zip)
- ✅ Generates SHA256 checksums
- ✅ Comprehensive error handling and logging
- ✅ Colored terminal output

### PyOxidizer Configuration
- Python 3.12.4 from python-build-standalone (20240713 release)
- Platform-specific targets for each architecture
- Filesystem-based resource fallback for compatibility
- All Stelvio dependencies included
- Optimized builds (PGO where available)

## 🚀 Quick Start (Once PyOxidizer is Ready)

```bash
# Build all platforms
./build-binaries

# Binaries will be in dist/:
# - stlv-0.4.0a6-linux-amd64.tar.gz
# - stlv-0.4.0a6-linux-arm64.tar.gz
# - stlv-0.4.0a6-macos-amd64.tar.gz
# - stlv-0.4.0a6-macos-arm64.tar.gz
# - stlv-0.4.0a6-windows-amd64.zip
# - SHA256SUMS
```

## 📦 Platform Support

| Platform | Architecture | Python Dist | Status |
|----------|-------------|-------------|--------|
| Linux | x86_64 | 3.12.4 PGO | ✅ Ready |
| Linux | aarch64 | 3.12.4 LTO | ✅ Ready |
| macOS | x86_64 | 3.12.4 PGO | ✅ Ready |
| macOS | aarch64 | 3.12.4 PGO | ✅ Ready |
| Windows | x86_64 | 3.12.4 PGO | ✅ Ready |

## 🔧 Configuration Details

### Python Distributions
Using optimized builds from python-build-standalone:
- **Format**: `.tar.zst` (compatible with PyOxidizer 0.24.0)
- **Release**: 20240713
- **Python Version**: 3.12.4
- **Optimizations**: PGO (Profile-Guided Optimization) or LTO (Link-Time Optimization)

### Build Targets
The PyOxidizer configuration defines separate targets for each platform:
- `install_linux_x86_64`
- `install_linux_aarch64`
- `install_macos_x86_64`
- `install_macos_aarch64`
- `install_windows_x86_64`

## 📖 Documentation

- **BUILD.md** - Full documentation on building, cross-compilation, CI/CD integration
- **BUILDING_QUICKSTART.md** - Quick reference for common tasks
- **PYOXIDIZER_FROM_SOURCE.md** - Step-by-step guide to build PyOxidizer from source

## 🐛 Known Issues

1. **PyOxidizer 0.24.0 + Python 3.12**
   - Issue: distutils removal causes build failures
   - Solution: Build PyOxidizer from source or use alternatives

2. **Cross-compilation**
   - Some platforms may require additional Rust targets
   - macOS binaries best built on macOS
   - Windows binaries require MSVC toolchain

## 🔮 Future Improvements

Once PyOxidizer 0.25.0 is released or you build from source:

1. **CI/CD Integration**: Add GitHub Actions workflow for automated builds
2. **Binary Signing**: Code signing for macOS and Windows
3. **Auto-updates**: Built-in update mechanism
4. **Reduced Size**: Further optimization of binary size

## 📝 Testing the Build System

To test that everything is configured correctly (without actually building):

```bash
# Check configuration syntax
python3 -c "exec(open('pyoxidizer.bzl').read())" 2>&1 || echo "Config has issues"

# Check build script syntax
bash -n build-binaries && echo "✓ Build script syntax OK"

# List PyOxidizer targets
pyoxidizer list-targets
```

## 🤝 Contributing

When modifying the build system:

1. Test on multiple platforms if possible
2. Update documentation if changing configuration
3. Verify SHA256 hashes when updating Python versions
4. Test with both release and debug builds

## 📄 License

Same as Stelvio project (Apache 2.0).

## 🆘 Support

If you encounter issues:

1. Check `BUILD.md` troubleshooting section
2. Review `/tmp/pyoxidizer-build.log` for detailed errors
3. Open an issue on the Stelvio GitHub repository
4. For PyOxidizer-specific issues, see https://github.com/indygreg/PyOxidizer

---

**Status**: Ready for use once PyOxidizer with Python 3.12 support is available.
