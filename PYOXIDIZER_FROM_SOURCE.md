# Building PyOxidizer from Source for Python 3.12 Support

PyOxidizer 0.24.0 (latest release) doesn't fully support Python 3.12 due to the `distutils` removal. The main branch has fixes for this, so we need to build from source.

## Prerequisites

- Rust toolchain (stable)
- Git
- Build tools (gcc, make, etc.)

## Installation Steps

### 1. Install Rust (if not already installed)

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
```

### 2. Clone PyOxidizer Repository

```bash
git clone https://github.com/indygreg/PyOxidizer.git
cd PyOxidizer
```

### 3. Build PyOxidizer

```bash
# Build the PyOxidizer CLI
cargo build --release --bin pyoxidizer

# The binary will be at: target/release/pyoxidizer
```

### 4. Install the Built Binary

```bash
# Copy to a location in your PATH
sudo cp target/release/pyoxidizer /usr/local/bin/

# Or add to PATH
export PATH="$(pwd)/target/release:$PATH"
```

### 5. Verify Installation

```bash
pyoxidizer --version
```

### 6. Build Stelvio Binaries

Once PyOxidizer from source is installed:

```bash
cd /path/to/stelvio
./build-binaries
```

## Alternative: Docker Build

If you don't want to install Rust locally, you can use Docker:

```bash
# Create a Dockerfile
cat > Dockerfile.pyoxidizer <<'EOF'
FROM rust:latest

RUN git clone https://github.com/indygreg/PyOxidizer.git /pyoxidizer
WORKDIR /pyoxidizer
RUN cargo build --release --bin pyoxidizer

FROM ubuntu:22.04
COPY --from=0 /pyoxidizer/target/release/pyoxidizer /usr/local/bin/
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*
EOF

# Build the image
docker build -f Dockerfile.pyoxidizer -t pyoxidizer-py312 .

# Use it to build binaries
docker run --rm -v $(pwd):/workspace -w /workspace pyoxidizer-py312 pyoxidizer build --release
```

## Troubleshooting

### Build Takes Too Long

PyOxidizer is a large Rust project. The first build can take 10-30 minutes depending on your system.

```bash
# Use more CPU cores
cargo build --release --bin pyoxidizer -j$(nproc)
```

### Compilation Errors

Ensure you have the latest Rust stable:

```bash
rustup update stable
```

### Missing Dependencies

On Debian/Ubuntu:

```bash
sudo apt-get install build-essential pkg-config libssl-dev
```

On macOS:

```bash
xcode-select --install
```

## Notes

- The main branch is under active development
- For production use, wait for official PyOxidizer 0.25.0 release
- Test your binaries thoroughly before distribution
