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
