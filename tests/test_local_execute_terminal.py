import errno
import json
import os
import re
import select
import struct
import subprocess
import sys
from pathlib import Path
from time import monotonic

from pytest import fixture, importorskip, mark

pytestmark = mark.skipif(sys.platform == "win32", reason="Requires POSIX pseudo-terminals")


def read_until(fd, marker, timeout=10):
    output = bytearray()
    deadline = monotonic() + timeout
    while marker not in output:
        remaining = deadline - monotonic()
        assert remaining > 0, f"Timed out waiting for {marker!r}: {bytes(output)!r}"
        if not select.select([fd], [], [], remaining)[0]:
            raise AssertionError(output.decode(errors="replace"))
        chunk = os.read(fd, 65536)
        assert chunk, bytes(output)
        output.extend(chunk)
    return bytes(output)


def read_available(fd, timeout=0.45):
    if not select.select([fd], [], [], timeout)[0]:
        return b""
    try:
        return os.read(fd, 65536)
    except OSError as error:
        if error.errno == errno.EIO:
            return b""
        raise


@fixture
def terminal_app(tmp_path, request):
    pty = importorskip("pty")
    fcntl = importorskip("fcntl")
    termios = importorskip("termios")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
    process = subprocess.Popen(  # noqa: S603  # Fixed test module and fixture-controlled arguments.
        [sys.executable, "-m", "tests.local_execute_terminal_app", str(tmp_path), request.param],
        cwd=Path(__file__).resolve().parents[1],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color", "PYTHONUNBUFFERED": "1"},
    )
    os.close(slave)
    try:
        yield process, master, tmp_path, request.param
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)


@mark.parametrize("terminal_app", ["changed", "unchanged"], indirect=True)
def test_blocking_input_starts_after_rich_completion_without_redraw(terminal_app):
    process, fd, project, scenario = terminal_app
    before_prompt = read_until(fd, b"test:")
    text = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", before_prompt).decode()
    assert text.index("Deployed in") < text.index("terminal-output") < text.index("test:")
    if scenario == "changed":
        assert "Nothing to deploy" not in text
        assert text.index("1 component (2 resources) deployed") < text.index("test:")
        assert text.rindex("terminal warning") < text.index("test:")
    else:
        assert text.index("Nothing to deploy") < text.index("test:")
    assert read_available(fd) == b""
    os.write(fd, b"ddd")
    assert read_until(fd, b"ddd") == b"ddd"
    assert read_available(fd) == b""
    os.write(fd, b"\n")
    output = read_until(fd, b"terminal-done")
    assert b"input=ddd\r\n" in output
    assert b"Finalizing" not in output
    assert process.wait(timeout=5) == 0
    assert json.loads((project / "report.json").read_text()) == {
        "input": "ddd",
        "cancellations": [],
        "errors": [],
        "following": 1,
        "lock_exists": False,
        "workdirs": [],
        "completed": True,
        "update_errors": [None],
    }


@mark.parametrize("terminal_app", ["unchanged"], indirect=True)
def test_ctrl_c_interrupts_blocked_local_input_once(terminal_app):
    process, fd, project, _ = terminal_app
    before_prompt = read_until(fd, b"test:")
    assert b"Deployed in" in before_prompt
    assert read_available(fd) == b""
    os.write(fd, b"\x03")
    output = read_until(fd, b"terminal-done")
    assert b"Finalizing" not in output
    assert process.wait(timeout=5) == 130
    assert json.loads((project / "report.json").read_text()) == {
        "input": None,
        "cancellations": [["interrupted", "local_execute"]],
        "errors": [],
        "following": 0,
        "lock_exists": False,
        "workdirs": [],
        "completed": True,
        "update_errors": [None],
    }


@mark.parametrize(
    "terminal_app",
    ["repl", "repl-unchanged", "repl-skip", "repl-no-callback", "repl-json", "repl-stream"],
    indirect=True,
)
def test_repl_terminal_context_interrupt_and_exit_without_redraw(terminal_app):
    process, fd, project, scenario = terminal_app
    callbacks_run = scenario not in ("repl-skip", "repl-no-callback")
    output = b""
    if callbacks_run:
        output = read_until(fd, b"test:")
        assert read_available(fd) == b""
        os.write(fd, b"ddd\n")
    output += read_until(fd, b">>> ")
    text = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output).decode()
    if scenario == "repl-json":
        before_prompt = text.split("test:")[0]
        summary = json.loads(before_prompt)
        assert summary["status"] == "success"
        assert summary["exit_code"] == 0
    elif scenario == "repl-stream":
        before_prompt = text.split("test:")[0]
        records = [json.loads(line) for line in before_prompt.splitlines()]
        assert records[0]["event"] == "start"
        assert records[-1]["event"] == "summary"
        assert records[-1]["status"] == "success"
    else:
        assert text.index("Deployed in") < text.index(">>> ")
    if callbacks_run:
        assert text.index("input=ddd") < text.index(">>> ")
    else:
        assert "test:" not in text
        assert "input=" not in text
    assert read_available(fd) == b""
    os.write(fd, b"print('context=', components.my_bucket.bucket_name, boto3.__name__)\n")
    evaluated = read_until(fd, b">>> ")
    assert b"context= demo-test-my-bucket-test-name boto3" in evaluated
    assert b"Finalizing" not in evaluated
    # Interrupt incomplete input: the shell keeps running and returns to its prompt.
    os.write(fd, b"(\n")
    read_until(fd, b"... ")
    os.write(fd, b"\x03")
    interrupted = read_until(fd, b">>> ")
    assert b"KeyboardInterrupt" in interrupted
    assert read_available(fd) == b""
    os.write(fd, b"exit(1)\n")
    finished = read_until(fd, b"terminal-done")
    assert b"Finalizing" not in finished
    assert process.wait(timeout=5) == 0
    report = json.loads((project / "report.json").read_text())
    assert report == {
        "input": "ddd" if callbacks_run else None,
        "cancellations": [],
        "errors": [],
        "following": 1 if callbacks_run else 0,
        "lock_exists": False,
        "workdirs": [],
        "completed": True,
        "update_errors": [None],
    }
