# PyOxidizer configuration for building stlv CLI binaries

# Python 3.12.4 distribution URLs and SHA256 from python-build-standalone (20240713)
# Using PGO-optimized builds where available (.tar.zst format for PyOxidizer 0.24 compatibility)
PYTHON_DISTRIBUTIONS = {
    "x86_64-unknown-linux-gnu": {
        "url": "https://github.com/indygreg/python-build-standalone/releases/download/20240713/cpython-3.12.4%2B20240713-x86_64-unknown-linux-gnu-pgo-full.tar.zst",
        "sha256": "6efe13ae191432589ec2427795bcd294a68ebd764ca8d597ab187be02dd8e47f",
    },
    "aarch64-unknown-linux-gnu": {
        "url": "https://github.com/indygreg/python-build-standalone/releases/download/20240713/cpython-3.12.4%2B20240713-aarch64-unknown-linux-gnu-lto-full.tar.zst",
        "sha256": "c7093d43470f07fce45a8fdc15e9e5bddd696174199083721cb1311ca5a100d1",
    },
    "x86_64-apple-darwin": {
        "url": "https://github.com/indygreg/python-build-standalone/releases/download/20240713/cpython-3.12.4%2B20240713-x86_64-apple-darwin-pgo-full.tar.zst",
        "sha256": "49587e5864844a99aac957cc6abbfa5d537f75975298b3829539988559091475",
    },
    "aarch64-apple-darwin": {
        "url": "https://github.com/indygreg/python-build-standalone/releases/download/20240713/cpython-3.12.4%2B20240713-aarch64-apple-darwin-pgo-full.tar.zst",
        "sha256": "78d2864ca629f490036ee06a52f7fbec325b97c2cb3f155456c65471d6fe47c8",
    },
    "x86_64-pc-windows-msvc": {
        "url": "https://github.com/indygreg/python-build-standalone/releases/download/20240713/cpython-3.12.4%2B20240713-x86_64-pc-windows-msvc-shared-pgo-full.tar.zst",
        "sha256": "3f1eb222d1d43d5d70551e8bd64d54aff40f754eb08600167c779f2f1c3559ac",
    },
}

def make_dist_for_target(target):
    """Create a Python distribution for a specific target triple."""
    if target in PYTHON_DISTRIBUTIONS:
        dist_info = PYTHON_DISTRIBUTIONS[target]
        return PythonDistribution(
            url = dist_info["url"],
            sha256 = dist_info["sha256"],
        )
    else:
        return default_python_distribution()

def make_exe_for_target(target):
    """Create an executable for a specific target triple."""
    dist = make_dist_for_target(target)
    
    policy = dist.make_python_packaging_policy()
    
    # Allow file-based resources (needed for some packages)
    policy.allow_files = True
    policy.file_scanner_emit_files = True
    policy.include_distribution_sources = False
    policy.include_distribution_resources = False
    policy.include_test = False
    
    # Resource handling - use filesystem for better compatibility
    policy.resources_location_fallback = "filesystem-relative:lib"

    python_config = dist.make_python_interpreter_config()
    
    # Configure Python interpreter
    python_config.run_command = "from stelvio.cli import cli; cli()"
    python_config.module_search_paths = ["$ORIGIN/lib"]
    
    exe = dist.to_python_executable(
        name = "stlv",
        packaging_policy = policy,
        config = python_config,
    )

    # For Python 3.12+, we need to install setuptools first to provide distutils compatibility
    exe.add_python_resources(exe.pip_install(["setuptools>=65.0.0"]))
    
    # Add the stelvio package and its dependencies
    # First install dependencies
    exe.add_python_resources(exe.pip_install([
        "pulumi==3.187.0",
        "pulumi-aws==7.2.0",
        "click",
        "appdirs",
        "requests",
        "rich>=14.0.0",
        "boto3",
        "pulumi-cloudflare==6.4.1",
    ]))
    
    # Then add the local package
    exe.add_python_resources(exe.pip_install(["."]))
    
    return exe

# Create target functions for each platform
def make_exe_linux_x86_64():
    return make_exe_for_target("x86_64-unknown-linux-gnu")

def make_exe_linux_aarch64():
    return make_exe_for_target("aarch64-unknown-linux-gnu")

def make_exe_macos_x86_64():
    return make_exe_for_target("x86_64-apple-darwin")

def make_exe_macos_aarch64():
    return make_exe_for_target("aarch64-apple-darwin")

def make_exe_windows_x86_64():
    return make_exe_for_target("x86_64-pc-windows-msvc")

def make_embedded_resources(exe):
    return exe.to_embedded_resources()

def make_install(exe):
    files = FileManifest()
    files.add_python_resource(".", exe)
    return files

def make_msi(exe):
    return exe.to_wix_msi_builder(
        "stlv",
        "Stelvio CLI",
        "0.4.0a6",
        "Stelvio Developers"
    )

# Register targets for each platform
register_target("exe_linux_x86_64", make_exe_linux_x86_64)
register_target("exe_linux_aarch64", make_exe_linux_aarch64)
register_target("exe_macos_x86_64", make_exe_macos_x86_64)
register_target("exe_macos_aarch64", make_exe_macos_aarch64)
register_target("exe_windows_x86_64", make_exe_windows_x86_64)

register_target("install_linux_x86_64", make_install, depends = ["exe_linux_x86_64"], default = True)
register_target("install_linux_aarch64", make_install, depends = ["exe_linux_aarch64"])
register_target("install_macos_x86_64", make_install, depends = ["exe_macos_x86_64"])
register_target("install_macos_aarch64", make_install, depends = ["exe_macos_aarch64"])
register_target("install_windows_x86_64", make_install, depends = ["exe_windows_x86_64"])

resolve_targets()
