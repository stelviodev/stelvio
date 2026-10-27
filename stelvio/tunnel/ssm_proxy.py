"""Fixed nonroot OpenSSH ProxyCommand: tokens are transient process inputs."""

import json
import os
import sys


def main() -> None:
    if not os.geteuid() or os.getuid() != os.geteuid():
        sys.exit(70)
    session = os.environ.pop("STLV_SSM_SESSION")
    plugin = os.environ.pop("STLV_SSM_PLUGIN")
    region = os.environ.pop("STLV_SSM_REGION")
    endpoint = os.environ.pop("STLV_SSM_ENDPOINT")
    target = os.environ.pop("STLV_SSM_TARGET")
    # The plugin's required native interface uses token arguments. No caller
    # prints commands or inspects process argv; no token file is ever written.
    os.execve(  # noqa: S606 - resolved nonroot plugin, fixed native interface
        plugin,
        [plugin, session, region, "StartSession", "", json.dumps({"Target": target}), endpoint],
        os.environ,
    )


if __name__ == "__main__":
    main()
