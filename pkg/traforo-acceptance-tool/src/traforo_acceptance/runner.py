"""Acceptance lifecycle, explicit stage commands, and frozen artifact selection."""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from functools import partial
from pathlib import Path

import boto3
import click

from . import aws, candidate, host, process, recovery
from .progress import heartbeat
from .report import generate
from .stages import AWS_TESTS, STAGES
from .store import Store, digest, event, now, write_json


def environment(root: Path, data):
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("STLV_TEST_", "TRA_ACCEPTANCE_"))
    }
    env.pop("STLV_TRAFORO_DEV_BINARY", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(
        root / "observer" if (root / "observer").exists() else Path(__file__).resolve().parents[1]
    )
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    env.pop("PYTEST_ADDOPTS", None)
    env["GOTOOLCHAIN"] = f"go{data['configuration']['go']}"
    env["TRA_ACCEPTANCE_RUN"] = str(root)
    env["STLV_TEST_AWS_PROFILE"] = data["configuration"]["profile"]
    env["STLV_TEST_AWS_REGION"] = data["configuration"]["region"]
    if data["configuration"].get("expected_account"):
        env["STLV_TEST_TUNNEL_AWS_ACCOUNT"] = data["configuration"]["expected_account"]
    for key, variable in (
        ("binary", "STLV_TRAFORO_DEV_BINARY"),
        ("binary", "STLV_TEST_TRAFORO_IMAGE"),
        ("wheel", "STLV_TEST_STELVIO_WHEEL"),
        ("sdist", "STLV_TEST_STELVIO_SDIST"),
    ):
        if key in data.get("artifacts", {}):
            env[variable] = data["artifacts"][key]["path"]
    return env


def verify(root: Path, data):
    if (
        data.get("tool_fingerprint")
        and candidate.tool_fingerprint(root / "observer/traforo_acceptance")
        != data["tool_fingerprint"]
    ):
        raise RuntimeError("Archived observer changed; retain evidence")
    if candidate.fingerprint(root / "source") != data["identity"]["fingerprint"]:
        raise RuntimeError("Frozen candidate changed; start a new run")
    for artifact in data.get("artifacts", {}).values():
        if digest(Path(artifact["path"])) != artifact["sha256"]:
            raise RuntimeError(f"Artifact changed: {artifact['path']}")


def artifact(path):
    return {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}


def authorize(root, data, env, execute, yes):
    candidate.validate(root / "source")
    before = host.snapshot()
    if int(before["os"].split(".")[0]) < 15:
        raise RuntimeError("Live acceptance requires macOS 15+")
    if before["administration_remnants"]:
        raise RuntimeError("Administration remnants exist; do not overwrite them")
    if before["helper"]:
        state = json.loads(
            execute(
                "preflight-helper",
                [str(root / "source/.venv/bin/python"), "-c", recovery.HELPER_STATUS],
                root / "source",
                env,
                30,
            )
        )
        if any(state.get(k) for k in ("owned", "uncertain", "mutating", "units")):
            raise RuntimeError("Existing live or uncertain helper state")
    elif before["state_exists"] or before["service"]:
        raise RuntimeError("Orphaned helper state/service exists")
    account = None
    if any(s.startswith("aws-") for s in data["selected"]):
        sdk = boto3.Session(
            profile_name=data["configuration"]["profile"],
            region_name=data["configuration"]["region"],
        )
        account = aws.Reader(sdk).call("sts", "get_caller_identity")["Account"]
        expected = data["configuration"].get("expected_account")
        if expected and account != expected:
            raise RuntimeError(f"AWS account mismatch: expected {expected}, got {account}")
        if yes and not expected:
            raise RuntimeError("Unattended live execution requires --expected-account")
    elif yes and not data["configuration"].get("expected_account"):
        raise RuntimeError("--yes requires --expected-account for live execution")
    click.echo(
        f"Live stages: {data['selected']}\nProfile: {data['configuration']['profile']}; "
        f"account: {account or 'not used'}; region: {data['configuration']['region']}\n"
        f"Local binary: {data['artifacts']['binary']}\n"
        "Will use administrator authorization, host routes/DNS, and (for AWS) billable proofs."
    )
    if not yes:
        if not click.get_text_stream("stdin").isatty():
            raise RuntimeError("Interactive confirmation required, or --yes --expected-account")
        click.confirm("Proceed with these live effects?", abort=True)
    if account:
        data["configuration"]["expected_account"] = account
        env["STLV_TEST_TUNNEL_AWS_ACCOUNT"] = account
        write_json(root / "shared-before.json", aws.shared(sdk))
    # Baseline is captured before any fixture/installer can change the host.
    write_json(root / "host-before.json", before)
    data["live_authorized"] = now()
    Store(root).save(data)
    # Validate the private pinned plugin, never a PATH executable.
    execute(
        "plugin",
        [
            str(root / "source/.venv/bin/python"),
            "-c",
            "from stelvio.tunnel.plugin import ensure_plugin,PLUGIN_VERSION; "
            "import subprocess; p=ensure_plugin(); print(p); "
            "v=subprocess.check_output([str(p),'--version'],text=True).strip(); "
            "assert v==PLUGIN_VERSION",
        ],
        root / "source",
        env,
        300,
    )


def stage_commands(name, root, env):
    source = root / "source"
    module = source / "pkg/traforo-client-go"
    python = str(source / ".venv/bin/python")
    pytest = [
        python,
        "-m",
        "pytest",
        "-p",
        "traforo_acceptance.pytest_plugin",
        "-n",
        "0",
        "--tb=long",
        f"--junitxml={root / (name + '.xml')}",
        f"--basetemp={root / f'cases-{list(STAGES).index(name):02d}'}",
    ]
    if name == "prepare":
        return [
            (["uv", "sync", "--locked"], source),
            (["go", "version"], module),
            (["goreleaser", "--version"], module),
        ]
    if name == "python":
        return [(pytest + ["tests", "-q"], source)]
    if name == "style":
        return [
            ([python, "-m", "ruff", "check", "stelvio", "tests"], source),
            ([python, "-m", "ruff", "format", "--check", "stelvio", "tests"], source),
        ]
    if name in {"go-unit", "go-race"}:
        return [
            (
                ["go", "test", "-mod=readonly", "-json"]
                + (["-race"] if name == "go-race" else [])
                + ["./..."],
                module,
            )
        ]
    if name == "go-vet":
        return [(["go", "vet", "-mod=readonly", "./..."], module)]
    if name == "go-lint":
        return [(["make", "lint"], module)]
    if name == "go-fuzz":
        return [
            (["make", target], module)
            for target in ("fuzz", "fuzz-snapshot", "fuzz-dns", "fuzz-control")
        ] + [
            (
                [
                    "go",
                    "test",
                    "-mod=readonly",
                    "./internal/diagnostics",
                    "-run=^$",
                    "-fuzz=^FuzzDecode$",
                    "-fuzztime=30s",
                    "-parallel=2",
                ],
                module,
            )
        ]
    if name == "build":
        return [([python, "pkg/traforo-client-go/scripts/build.py", "--build-only"], source)]
    if name == "distribution":
        return [(["uv", "build", "--out-dir", str(root / "dist")], source)]
    if name == "interop":
        env["STLV_TEST_SSH_INTEROP"] = "1"
        return [
            (
                pytest
                + [
                    "tests/tunnel/test_distribution_acceptance.py",
                    "tests/tunnel/test_runtime.py",
                    "tests/tunnel/test_transport.py",
                    "-vv",
                ],
                source,
            )
        ]
    if name == "native":
        return [
            (
                pytest
                + [
                    f"tests/integration/{p}"
                    for p in (
                        "test_tunnel_host.py",
                        "test_tunnel_dns.py",
                        "test_tunnel_observation.py",
                        "test_tunnel_diagnostics.py",
                    )
                ]
                + [
                    "--integration-macos-privileged",
                    "-x",
                    "-vv",
                    "-k",
                    "not pinned_release_to_local_candidate_upgrade",
                ],
                source,
            )
        ]
    if name in AWS_TESTS:
        return [
            (
                pytest
                + [
                    f"tests/integration/test_tunnel.py::{AWS_TESTS[name]}",
                    "--integration-macos-privileged",
                    "--integration-tunnel",
                    "-x",
                    "-vv",
                ],
                source,
            )
        ]
    raise RuntimeError(f"Unknown stage {name}")


def safe_ssh_path(path):
    for parent in (path, *path.parents):
        info = parent.stat()
        if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise RuntimeError(
                f"OpenSSH StrictModes rejects writable/foreign ancestor {parent}; "
                "use the default run directory in the macOS private user temporary folder"
            )


def execute_stages(root: Path, yes=False):
    store = Store(root)
    data = store.data
    env = environment(root, data)
    # Defaults can be overridden per stage. Finalization is never selectable away.
    deadlines = data["configuration"].get("timeouts", {})
    try:
        verify(root, data)
        for name in data["selected"]:
            if data["results"].get(name, {}).get("outcome") == "passed":
                continue
            if any(r.get("outcome") != "passed" for r in data["results"].values()):
                raise RuntimeError("A previous stage failed or was interrupted; start a new run")
            candidate.validate(root / "source")
            stage = STAGES[name]
            started = time.monotonic()
            data["results"][name] = {"outcome": "running", "started": now()}
            store.save(data)
            event(root, "stage_start", stage=name, estimate=stage.estimate)
            process.console.print(
                f"[{data['selected'].index(name) + 1}/{len(data['selected'])}] "
                f"{name}: starting; estimate {stage.estimate}"
            )
            env["TRA_ACCEPTANCE_STAGE"] = name
            if stage.group in {"native", "aws"}:
                # Fixtures take the shared root-owned host lock; the runner must
                # not hold that lock while pytest acquires it.
                if not data.get("live_authorized"):
                    authorize(root, data, env, partial(process.run, root), yes)
                if stage.group == "aws":
                    process.run(
                        root,
                        "pymongo",
                        [
                            "uv",
                            "pip",
                            "install",
                            "--python",
                            str(root / "source/.venv/bin/python"),
                            "pymongo",
                        ],
                        root / "source",
                        env,
                        300,
                    )
            outputs = []
            deadline = deadlines.get(name, stage.timeout)
            if name == "interop":
                safe_ssh_path(root)
            for argv, cwd in stage_commands(name, root, env):
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"{name} exhausted its stage deadline")
                outputs.append(process.run(root, name, argv, cwd, env, remaining))
            if name == "prepare":
                if f"go{data['configuration']['go']} " not in outputs[1]:
                    raise RuntimeError("Unexpected Go toolchain")
                if "2.18.3" not in outputs[2]:
                    raise RuntimeError("GoReleaser 2.18.3 required")
                data["tools"] = {
                    "go": outputs[1].strip(),
                    "goreleaser": outputs[2].strip(),
                    "python": platform.python_version(),
                    "host": platform.platform(),
                }
            if name == "build":
                manifests = list((root / "source/pkg/traforo-client-go/build").glob("*/build.json"))
                if len(manifests) != 1:
                    raise RuntimeError("Expected exactly one validated local build bundle")
                manifest = manifests[0]
                provenance = json.loads(manifest.read_text())
                if (
                    provenance["commit"] != data["identity"]["commit"]
                    or len(provenance["artifacts"]) != 2
                ):
                    raise RuntimeError("Build provenance mismatch")
                artifacts = data.setdefault("artifacts", {})
                for record in provenance["artifacts"]:
                    image = manifest.parent / record["name"]
                    if digest(image) != record["sha256"]:
                        raise RuntimeError("Build artifact digest mismatch")
                    artifacts[record["architecture"]] = artifact(image)
                machine = platform.machine()
                artifacts["binary"] = artifacts[machine]
                artifacts["provenance"] = artifact(manifest)
            if name == "distribution":
                for key, pattern in (("wheel", "*.whl"), ("sdist", "*.tar.gz")):
                    paths = list((root / "dist").glob(pattern))
                    if len(paths) != 1:
                        raise RuntimeError(f"Expected exactly one {key}")
                    data.setdefault("artifacts", {})[key] = artifact(paths[0])
            verify(root, data)
            data["results"][name].update(
                outcome="passed", runtime=round(time.monotonic() - started, 3)
            )
            store.save(data)
            event(root, "stage_end", stage=name, **data["results"][name])
            env = environment(root, data)
    except (Exception, KeyboardInterrupt) as error:
        import traceback

        if "name" in locals() and data["results"].get(name, {}).get("outcome") == "running":
            data["results"][name].update(
                outcome="failed", runtime=round(time.monotonic() - started, 3)
            )
        store.save(data)
        event(
            root,
            "failure",
            stage=locals().get("name", "resume-verification"),
            error=str(error),
            trace=traceback.format_exc(),
        )
        process.console.print(f"Failed: {error}", markup=False)
    finally:
        # Fresh data avoids overwriting durable audit/recovery changes.
        def execute(stage, argv, cwd, environment, timeout):
            verify(root, store.data)
            return process.run(root, stage, argv, cwd, environment, timeout)

        with heartbeat(root, "finalization"):
            try:
                if store.data.get("live_authorized"):
                    verify(root, store.data)
                    recovery.recover(root, execute, env)
            except (Exception, KeyboardInterrupt) as error:
                event(root, "failure", stage="recovery", error=str(error))
            try:
                recovery.audit(root, execute, env)
            except (Exception, KeyboardInterrupt) as error:
                event(root, "failure", stage="audit", error=str(error))
                final = store.data
                final["audit"] = {"outcome": "unknown", "error": str(error)}
                store.save(final)
        # Preserve crash/fuzz reproducer files even when Go wrote untracked fixtures.
        for path in (root / "source/pkg/traforo-client-go").glob("**/testdata/fuzz/**/*"):
            if path.is_file():
                target = root / "fuzz" / path.relative_to(root / "source/pkg/traforo-client-go")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        report = generate(root)
        process.console.print(f"{report['outcome']}: {root / 'report.md'}")
    return report
