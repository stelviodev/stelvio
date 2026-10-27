"""Bounded commands with durable reproduction records and regular heartbeats."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from rich.console import Console

from .store import event, events, now, redact, write_json

console = Console()


def identity(pid: int) -> dict:
    process = psutil.Process(pid)
    return {"pid": pid, "created": process.create_time(), "uid": process.uids().real}


def signal_owned(record: dict, sig: int) -> None:
    try:
        current = identity(record["pid"])
    except psutil.NoSuchProcess:
        return
    if current != record or current["uid"] != os.getuid():
        raise RuntimeError("Process identity changed; refusing to signal it")
    os.killpg(record["pid"], sig)


class CommandFailure(RuntimeError):
    pass


def run(root: Path, stage: str, argv: list[str], cwd: Path, env: dict, timeout=3600) -> str:
    directory = root / "commands" / f"{time.time_ns()}-{stage}"
    directory.mkdir(parents=True, mode=0o700)
    record = {
        "stage": stage,
        "argv": argv,
        "executable": shutil.which(argv[0], path=env.get("PATH")),
        "cwd": str(cwd),
        "started": now(),
        "timeout_seconds": timeout,
        "environment": {
            k: v
            for k, v in env.items()
            if k
            in {
                "PATH",
                "GOTOOLCHAIN",
                "STLV_TRAFORO_DEV_BINARY",
                "STLV_TEST_TRAFORO_IMAGE",
                "STLV_TEST_AWS_PROFILE",
                "STLV_TEST_AWS_REGION",
                "STLV_TEST_TUNNEL_AWS_ACCOUNT",
                "STLV_TEST_STELVIO_WHEEL",
                "STLV_TEST_STELVIO_SDIST",
                "STLV_TEST_SSH_INTEROP",
            }
        },
    }
    write_json(directory / "command.json", record)
    event(root, "command_start", **record, evidence=str(directory))
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    owner = identity(process.pid)
    record["process"] = owner
    write_json(directory / "command.json", record)
    messages = queue.Queue(maxsize=1024)

    def read():
        assert process.stdout is not None
        try:
            for line in iter(process.stdout.readline, b""):
                messages.put(line.decode(errors="replace"))
        finally:
            process.stdout.close()
            messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    last_output = last_heartbeat = started
    output = deque(maxlen=20000)
    stopped = None
    deadline_signal = None
    eof = False
    log = directory / "output.log"
    try:
        with log.open("w") as file, (root / "run.log").open("a") as combined:
            log.chmod(0o600)
            (root / "run.log").chmod(0o600)
            while not eof or process.poll() is None:
                try:
                    line = messages.get(timeout=0.2)
                    if line is None:
                        eof = True
                    else:
                        last_output = time.monotonic()
                        line = redact(line)
                        output.append(line[-65536:])
                        if stage.startswith("go-"):
                            try:
                                go_event = json.loads(line)
                            except ValueError:
                                pass
                            else:
                                if go_event.get("Action") in {"fail", "pass", "skip"}:
                                    event(root, "go_result", stage=stage, result=go_event)
                        stamped = f"{now()} [{stage}] {line}"
                        file.write(stamped)
                        combined.write(stamped)
                        file.flush()
                        combined.flush()
                except queue.Empty:
                    pass
                current = time.monotonic()
                if current - last_heartbeat >= 10:
                    event(
                        root,
                        "heartbeat",
                        stage=stage,
                        elapsed=current - started,
                        last_output_age=current - last_output,
                        deadline_seconds=timeout,
                    )
                    console.print(
                        f"[{now()}] {stage}: {current - started:.0f}s elapsed; "
                        f"deadline {timeout:.0f}s; output {current - last_output:.0f}s ago",
                        markup=False,
                    )
                    case = next(
                        (
                            r
                            for r in reversed(events(root))
                            if r["kind"] in {"test_start", "test_result"}
                            and r.get("stage") == stage
                        ),
                        None,
                    )
                    if case:
                        console.print(
                            f"  {case['nodeid']} [{case.get('phase', 'running')}]", markup=False
                        )
                    last_heartbeat = current
                if current - started > timeout and deadline_signal is None:
                    stopped = "timeout"
                    deadline_signal = current
                    signal_owned(owner, signal.SIGINT)
                if deadline_signal is not None and current - deadline_signal > 90:
                    signal_owned(owner, signal.SIGTERM)
                if deadline_signal is not None and current - deadline_signal > 120:
                    signal_owned(owner, signal.SIGKILL)
                # A descendant that retained stdout must not keep the runner stuck.
                if process.poll() is not None and current - last_output > 5:
                    break
    except KeyboardInterrupt:
        stopped = "cancelled"
        signal_owned(owner, signal.SIGINT)
        try:
            process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            signal_owned(owner, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                signal_owned(owner, signal.SIGKILL)
                process.wait(timeout=5)
    finally:
        record.update(
            finished=now(),
            runtime_seconds=round(time.monotonic() - started, 3),
            exit_code=process.poll(),
            stopped=stopped,
        )
        write_json(directory / "command.json", record)
        event(root, "command_end", **record, evidence=str(directory))
    if stopped or process.returncode != 0:
        tail = "".join(list(output)[-20:])
        event(root, "command_failure_output", stage=stage, tail=tail, evidence=str(directory))
        raise CommandFailure(f"{stage}: {stopped or f'exit {process.returncode}'}; see {directory}")
    return "".join(output)
