"""Finite real loopback peer; propagate worker failures and close every socket."""

import socket
from contextlib import contextmanager
from queue import Queue
from threading import Thread


def receive(connection, length):
    result = bytearray()
    while len(result) < length:
        part = connection.recv(length - len(result))
        if not part:
            raise ConnectionError("Fixture peer ended early")
        result.extend(part)
    return bytes(result)


@contextmanager
def tcp_peer(handle, *, connections=1):
    errors = Queue()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def run():
            try:
                for _ in range(connections):
                    with listener.accept()[0] as connection:
                        connection.settimeout(5)
                        handle(connection)
            except BaseException as error:
                errors.put(error)

        thread = Thread(target=run)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            thread.join(6)
            assert not thread.is_alive(), "Fixture peer did not stop within its budget"
            if not errors.empty():
                raise errors.get()
