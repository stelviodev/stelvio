"""Heartbeats also cover bounded SDK inventories and recovery outside subprocesses."""

from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic

from .process import console
from .store import event, now


@contextmanager
def heartbeat(root, label):
    stop = Event()
    start = monotonic()

    def pulse():
        while not stop.wait(10):
            elapsed = monotonic() - start
            event(root, "heartbeat", stage=label, elapsed=elapsed)
            console.print(
                f"[{now()}] {label}: {elapsed:.0f}s elapsed; waiting for bounded recovery/audit",
                markup=False,
            )

    thread = Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
