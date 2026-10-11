# Python support

Stelvio supports the latest three stable Python minor releases. All three must be
tested in CI. Keep the supported versions and test matrices aligned as new stable
releases arrive.

Test the next Python minor release as experimental as soon as a prerelease is
available. Experimental test failures do not block CI, but should be investigated
before that version becomes stable. A prerelease does not count toward the three
supported stable versions.

## Lambda runtime defaults

The previous latest version means the stable Python minor release immediately
before the latest stable release.

- The dev mode Lambda stub deploys on the previous latest version, independently
  of the user's Python version or configured Function runtime.
- A Function with no explicit `runtime` defaults to the major/minor version of
  the Python interpreter running `stlv`. If that version is outside Stelvio's
  supported Lambda runtime range, use the previous latest version as the fallback.
- Layers use the same runtime default so their dependency packages and compatibility
  checks agree with Functions. An explicit `runtime` overrides the default.

When a new Python release becomes stable, update the supported versions, test
matrices, dev stub runtime and fallback together. Keep dependency packaging and
runtime tests aligned with those choices. Experimental releases do not change
the stub or fallback.

Changing the interpreter running `stlv` can change an unpinned Function or Layer
runtime on the next deploy. Document that behavior and show users how to pin it
with `runtime=`.
