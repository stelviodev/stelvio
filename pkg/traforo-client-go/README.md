# Traforo reference snapshot

Canonical source, Go tests, and release builds live in
https://github.com/stelviodev/traforo.

This directory is retained for architecture/code reference only. Stelvio does not
build it or include it in wheel/sdist. Do not use this snapshot to certify a released
executable. There is no local release builder.

Stelvio pins release download URLs and SHA-256 values in `stelvio/tunnel/assets.py`.
See `stelvio/tunnel/testing.md` for Python integration, downloaded-release, native
host lifecycle and AWS acceptance instructions.
