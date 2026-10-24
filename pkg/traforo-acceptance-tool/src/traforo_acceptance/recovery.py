"""Owned recovery and independent audit orchestration."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import boto3

from . import aws, host
from .lambda_aws import capture_proof, identity, is_lambda, recover_apis
from .ownership import process_record
from .store import Store, digest, event, events, write_json

# Adapter operations use production transactions, never the candidate's audit assertions.
HELPER_STATUS = """
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict
from stelvio.tunnel.helper_client import NativeHelper
print(json.dumps(asdict(NativeHelper().status()),default=str))
"""


def proofs(root):
    result = {}
    for row in events(root):
        if row["kind"] == "proof_intent":
            result[row["app"]] = {
                k: row[k]
                for k in ("app", "env", "project", "account", "profile", "region", "suite")
                if k in row
            }
    return result


def sdk_for(proof):
    return boto3.Session(profile_name=proof["profile"], region_name=proof["region"])


def prior_snapshots(root, app):
    return [
        r["snapshot"] for r in events(root) if r["kind"] == "proof_inventory" and r["app"] == app
    ]


def audit(root: Path, execute, env):
    store = Store(root)
    data = store.data
    findings = {}
    if (root / "host-before.json").exists():
        try:
            before = json.loads((root / "host-before.json").read_text())
            after = host.snapshot()
            write_json(root / "host-after.json", after)
            checks = host.compare(before, after)
            if after["helper"]:
                state = json.loads(
                    execute(
                        "audit-status",
                        [str(root / "source/.venv/bin/python"), "-c", HELPER_STATUS],
                        root / "source",
                        env,
                        30,
                    )
                )
                checks["helper_idle"] = not any(
                    state.get(k) for k in ("owned", "uncertain", "mutating", "units")
                )
            else:
                checks["helper_idle"] = not after["state_exists"]
            findings["host"] = {
                "checks": checks,
                "outcome": "certified" if all(checks.values()) else "remaining",
            }
        except Exception as error:
            findings["host"] = {"outcome": "unknown", "error": str(error)}
    for app, proof in proofs(root).items():
        try:
            snapshot = capture_proof(root, sdk_for(proof), proof, prior_snapshots(root, app))
            event(root, "proof_inventory", **proof, snapshot=snapshot)
            findings[app] = {
                "outcome": "unknown"
                if snapshot.get("unknown")
                else "remaining"
                if snapshot["remaining"]
                else "certified",
                **snapshot,
            }
        except Exception as error:
            findings[app] = {"outcome": "unknown", "error": str(error)}
    if (root / "shared-before.json").exists():
        try:
            sdk = boto3.Session(
                profile_name=data["configuration"]["profile"],
                region_name=data["configuration"]["region"],
            )
            after = aws.shared(sdk)
            before = json.loads((root / "shared-before.json").read_text())
            findings["shared_appsync"] = {
                "outcome": "certified" if all(r in after for r in before) else "remaining"
            }
        except Exception as error:
            findings["shared_appsync"] = {"outcome": "unknown", "error": str(error)}
    # An inventory gap before destruction means untagged volumes/secrets cannot
    # be certified by an empty post-destroy namespace alone.
    records = events(root)
    gaps = [r for r in records if r["kind"] == "inventory_error"]
    missing = [
        app
        for app in proofs(root)
        if not any(r["kind"] == "proof_inventory" and r["app"] == app for r in records)
    ]
    if missing:
        findings["missing_inventory"] = {"outcome": "unknown", "apps": missing}
    if gaps:
        findings["inventory_gaps"] = {"outcome": "unknown", "evidence": gaps}
    data = store.data
    data["audit"] = {
        "scope": "registered host/AWS effects"
        if data.get("live_authorized")
        else "no live stages activated",
        "outcome": "certified"
        if all(v["outcome"] == "certified" for v in findings.values())
        else "unknown-or-remaining",
        "findings": findings,
    }
    data.setdefault("audit_history", []).append(data["audit"])
    store.save(data)
    event(root, "audit_result", **data["audit"])
    return data["audit"]


def recover_lambda_application(root, sdk, proof, project, execute, env):
    """API failure must not skip application destruction; no deploy means no destroy."""
    actions = []
    if not any(
        r["kind"] == "application_deploy_intent" and r["app"] == proof["app"] for r in events(root)
    ):
        return [{"app": proof["app"], "application": "not-started", "outcome": "completed"}]
    try:
        snapshot = capture_proof(root, sdk, proof, prior_snapshots(root, proof["app"]))
        event(root, "proof_inventory", **proof, snapshot=snapshot)
    except Exception as error:
        event(root, "inventory_error", **proof, error=str(error))
        actions.append(
            {
                "app": proof["app"],
                "operation": "pre-destroy inventory",
                "outcome": "failed",
                "error": str(error),
            }
        )
    try:
        execute(
            "recover-destroy",
            [str(root / "source/.venv/bin/stlv"), "destroy", proof["env"], "--yes"],
            project,
            env,
            3600,
        )
        actions.append({"app": proof["app"], "operation": "destroy", "outcome": "completed"})
    except Exception as error:
        actions.append(
            {"app": proof["app"], "operation": "destroy", "outcome": "failed", "error": str(error)}
        )
    try:
        current = capture_proof(root, sdk, proof, prior_snapshots(root, proof["app"]))
        event(root, "proof_inventory", **proof, snapshot=current)
        if current["remaining"].get("functions") or current["remaining"].get("captured_functions"):
            raise RuntimeError("Functions remain; refusing log deletion")
        allowed = {
            f"/aws/lambda/{name}"
            for prior in prior_snapshots(root, proof["app"])
            for name in prior.get("functions", [])
        }
        reader = aws.Reader(sdk)
        for name in current["remaining"].get("log_groups", []):
            if name not in allowed:
                raise RuntimeError(f"Untracked log group retained: {name}")
            event(
                root,
                "recovery_aws_call",
                **proof,
                service="logs",
                operation="delete_log_group",
                arguments={"logGroupName": name},
            )
            reader.call("logs", "delete_log_group", logGroupName=name)
    except Exception as error:
        actions.append(
            {
                "app": proof["app"],
                "operation": "post-destroy inventory/logs",
                "outcome": "failed",
                "error": str(error),
            }
        )
    return actions


def recover(root: Path, execute, env):
    store = Store(root)
    data = store.data
    actions = []
    for row in events(root):
        if row["kind"] != "proof_process":
            continue
        record = row["process"]
        try:
            try:
                current = process_record(record["pid"])
            except subprocess.CalledProcessError:
                continue  # already reaped; independently check for orphaned children
            if current != record:
                raise RuntimeError("PID identity changed; refusing to signal")
            os.killpg(record["group"], signal.SIGINT)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    if process_record(record["pid"]) != record:
                        break
                except subprocess.CalledProcessError:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Owned CLI did not stop; retain evidence for manual recovery")
            actions.append({"process": record, "outcome": "completed"})
        except Exception as error:
            actions.append({"process": record, "outcome": "failed", "error": str(error)})
    for app, proof in proofs(root).items():
        try:
            aws.validate_proof(proof)
            project = Path(proof["project"])
            if not project.resolve().is_relative_to(root.resolve()) or project.is_symlink():
                raise RuntimeError("Proof project is outside the run directory")
            if json.loads((project / ".acceptance-owner.json").read_text()) != proof:
                raise RuntimeError("Proof ownership marker changed")
            sdk = sdk_for(proof)
            if is_lambda(proof):
                identity(aws.Reader(sdk), proof)
                actions.extend(recover_apis(root, sdk, proof))
                actions.extend(recover_lambda_application(root, sdk, proof, project, execute, env))
                continue
            snapshot = aws.capture(sdk, proof, prior_snapshots(root, app))
            event(root, "proof_inventory", **proof, snapshot=snapshot)
            if snapshot["remaining"]:
                execute(
                    "recover-destroy",
                    [str(root / "source/.venv/bin/stlv"), "destroy", proof["env"], "--yes"],
                    project,
                    env,
                    3600,
                )
                # Delete only exact function log groups captured before destruction,
                # and only after all proof functions have disappeared.
                current = aws.capture(sdk, proof, [snapshot])
                if current["remaining"].get("functions") or current["remaining"].get(
                    "captured_functions"
                ):
                    raise RuntimeError("Functions remain; refusing log deletion")
                allowed = {
                    f"/aws/lambda/{name}"
                    for prior in prior_snapshots(root, app)
                    for name in prior.get("functions", [])
                }
                reader = aws.Reader(sdk)
                for name in current["remaining"].get("log_groups", []):
                    if name not in allowed:
                        raise RuntimeError(f"Untracked log group retained: {name}")
                    event(
                        root,
                        "recovery_aws_call",
                        service="logs",
                        operation="delete_log_group",
                        arguments={"logGroupName": name},
                        **proof,
                    )
                    reader.call("logs", "delete_log_group", logGroupName=name)
            actions.append({"app": app, "outcome": "completed"})
        except Exception as error:
            actions.append({"app": app, "outcome": "failed", "error": str(error)})
    intents = [r for r in events(root) if r["kind"] == "host_intent"]
    if intents:
        # Restore the first fixture's original installation, not an intermediate one.
        intent = intents[0]
        selected = data.get("artifacts", {}).get("binary")
        images = {selected["sha256"]: selected["path"]} if selected else {}
        for row in events(root):
            if row["kind"] == "admin_image_intent":
                images.setdefault(row["digest"], row["path"])
        if intent.get("backup"):
            images[intent["digest"]] = intent["backup"]
        try:
            backup = intent["backup"]
            if backup and (
                not Path(backup).resolve().is_relative_to(root)
                or digest(Path(backup)) != intent["digest"]
            ):
                raise RuntimeError("Invalid original image backup")
            execute(
                "recover-helper",
                [
                    str(root / "source/.venv/bin/python"),
                    "-m",
                    "traforo_acceptance.admin_adapter",
                    backup or "",
                    intent["digest"] or "",
                    json.dumps(images),
                ],
                root / "source",
                env,
                240,
            )
            actions.append({"helper": "original installation", "outcome": "completed"})
        except Exception as error:
            actions.append(
                {"helper": "original installation", "outcome": "failed", "error": str(error)}
            )
    data = store.data
    data.setdefault("recovery", []).append(actions)
    store.save(data)
    for action in actions:
        event(root, "recovery", **action)
    return actions
