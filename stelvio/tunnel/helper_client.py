"""Nonprivileged client for the installed native macOS helper."""

from __future__ import annotations

import array
import ctypes
import os
import socket
import stat
import struct
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stelvio.tunnel.helper_protocol import ResolverEndpoint

STATE_DIRECTORY = Path("/Library/Application Support/Stelvio/tunnel")
CONTROL_SOCKET = STATE_DIRECTORY / "helper.sock"
HELPER_ABI = 1
CARRIER_VERSION = 1
MAX_REPLY = 256
SOCKET_MODE = 0o666
_INSPECTION = struct.Struct("!8sIII")
_ACQUISITION = struct.Struct("!16sI")
_UNIT = struct.Struct("!IQB")
MAX_UNITS = 8
_REPLY = struct.Struct("!8sHHI")


class HelperError(RuntimeError):
    """The native helper rejected a request or its reply could not be trusted."""


class HelperBusyError(HelperError):
    """The authenticated helper has another active operation or host owner."""


class HelperStatus(IntEnum):
    OK = 0
    BUSY = 1
    INVALID = 2
    UNCERTAIN = 3
    UNAUTHORIZED = 4


class HelperUnitStatus(IntEnum):
    INACTIVE = 0
    ACTIVE = 1
    FAILED = 2
    MUTATING = 3
    RETAINED = 4


@dataclass(frozen=True)
class HelperUnitState:
    unit: str
    generation: int
    status: HelperUnitStatus


@dataclass(frozen=True)
class HelperInspection:
    owned: bool
    uncertain: bool
    mutating: bool
    units: tuple[HelperUnitState, ...] = ()


def _platform() -> None:
    if sys.platform != "darwin":
        raise HelperError("VPC dev networking requires the supported macOS helper")


def _root_peer(connection: socket.socket) -> None:
    # Lazy platform loading keeps imports usable for ordinary non-VPC sessions.
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    library.getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if library.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) or uid.value:
        raise HelperError("The helper socket did not authenticate a root server")


def _socket_path() -> None:
    for path in (
        Path("/Library"),
        Path("/Library/Application Support"),
        STATE_DIRECTORY.parent,
        STATE_DIRECTORY,
    ):
        info = path.lstat()
        if info.st_uid or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise HelperError("The helper namespace has unsafe ownership or permissions")
    info = CONTROL_SOCKET.lstat()
    if info.st_uid or not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o777 != SOCKET_MODE:
        raise HelperError("The helper control socket has unsafe ownership or permissions")


def _close_descriptors(descriptors: Sequence[int]) -> None:
    for descriptor in descriptors:
        with suppress(OSError):
            os.close(descriptor)


def _controls(controls: list, descriptors: list[int]) -> bool:
    valid = True
    for level, kind, contents in controls:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            valid = False
            continue
        values = array.array("i")
        aligned = len(contents) - len(contents) % values.itemsize
        values.frombytes(contents[:aligned])
        # Register every received descriptor before any fallible flag update.
        descriptors.extend(values)
        for descriptor in values:
            os.set_inheritable(descriptor, False)
        valid = valid and aligned == len(contents)
    return valid


def _message(valid_control: bool, flags: int, descriptors: list[int], data: bytes) -> None:
    if not valid_control or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(descriptors) > 1:
        raise HelperError("The helper returned invalid descriptor metadata")
    if not data:
        raise HelperError("The helper closed an incomplete reply")


def _header(packet: bytes | bytearray) -> tuple[HelperStatus, int]:
    magic, status, reserved, size = _REPLY.unpack(packet)
    if magic != b"STLVREP1" or reserved or size > MAX_REPLY:
        raise HelperError("The helper returned an incompatible reply")
    try:
        return HelperStatus(status), size
    except ValueError as error:
        raise HelperError("The helper returned an unknown status") from error


def _success(response: HelperStatus) -> None:
    if response == HelperStatus.BUSY:
        raise HelperBusyError("Native helper request failed: busy")
    if response != HelperStatus.OK:
        raise HelperError(f"Native helper request failed: {response.name.lower()}")


def _receive(connection: socket.socket) -> tuple[bytes, list[int]]:
    packet = bytearray()
    descriptors: list[int] = []
    target = _REPLY.size
    response = HelperStatus.OK
    try:
        while len(packet) < target:
            data, controls, flags, _ = connection.recvmsg(target - len(packet), 4096)
            _message(_controls(controls, descriptors), flags, descriptors, data)
            packet.extend(data)
            if len(packet) == _REPLY.size:
                response, size = _header(packet)
                target += size
        _success(response)
        return bytes(packet[_REPLY.size :]), descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _acquisition(payload: bytes, descriptors: list[int]) -> bytes:
    if len(payload) != _ACQUISITION.size or len(descriptors) != 1:
        raise HelperError("The helper returned an incomplete acquisition")
    capability, version = _ACQUISITION.unpack(payload)
    if not any(capability) or version != CARRIER_VERSION:
        raise HelperError("The helper packet carrier is incompatible")
    return capability


def _carrier(carrier: socket.socket) -> None:
    if carrier.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_DGRAM:
        raise HelperError("The helper returned an invalid packet carrier")
    carrier.setblocking(False)


class NativeHelper:
    """Control calls originate from the same process that holds the EOF lease."""

    def __init__(self, *, timeout: float = 120) -> None:
        self.timeout = timeout

    def connect(self) -> socket.socket:
        _platform()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            _socket_path()
            connection.connect(str(CONTROL_SOCKET))
            _root_peer(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def request(self, request: HelperRequest) -> bytes:
        with self.connect() as connection:
            connection.sendall(request.encode())
            payload, descriptors = _receive(connection)
            if descriptors:
                _close_descriptors(descriptors)
                raise HelperError("Only acquisition may return a packet descriptor")
            return payload

    def inspect(self) -> HelperInspection:
        payload = self.request(HelperRequest(HelperOperation.INSPECT))
        if len(payload) <= _INSPECTION.size:
            raise HelperError("The helper returned an incompatible inspection")
        magic, abi, carrier, flags = _INSPECTION.unpack(payload[: _INSPECTION.size])
        if magic != b"STLVHLP1" or abi != HELPER_ABI or carrier != CARRIER_VERSION or flags & ~7:
            raise HelperError("The installed helper is incompatible with this package")
        count = payload[_INSPECTION.size]
        if count > MAX_UNITS or len(payload) != _INSPECTION.size + 1 + count * _UNIT.size:
            raise HelperError("The helper returned an invalid unit inventory")
        units = []
        seen = set()
        for offset in range(_INSPECTION.size + 1, len(payload), _UNIT.size):
            identity, generation, status = _UNIT.unpack_from(payload, offset)
            if not identity or not generation or identity in seen:
                raise HelperError("The helper returned an invalid unit identity")
            try:
                value = HelperUnitStatus(status)
            except ValueError as error:
                raise HelperError("The helper returned an invalid unit status") from error
            units.append(HelperUnitState(f"{identity:08x}", generation, value))
            seen.add(identity)
        return HelperInspection(bool(flags & 1), bool(flags & 2), bool(flags & 4), tuple(units))

    def reconcile(self) -> None:
        self.request(HelperRequest(HelperOperation.RECONCILE))

    def acquire(self, session: str) -> NativeLease:
        connection = self.connect()
        descriptors = []
        try:
            connection.sendall(HelperRequest(HelperOperation.ACQUIRE, session=session).encode())
            payload, descriptors = _receive(connection)
            capability = _acquisition(payload, descriptors)
            carrier = socket.socket(fileno=descriptors[0])
            descriptors.clear()
            try:
                _carrier(carrier)
            except BaseException:
                carrier.close()
                raise
            return NativeLease(self, session, capability, connection, carrier)
        except BaseException:
            _close_descriptors(descriptors)
            connection.close()
            raise


@dataclass
class NativeLease:
    helper: NativeHelper
    session: str
    capability: bytes = field(repr=False)
    connection: socket.socket = field(repr=False)
    carrier: socket.socket = field(repr=False)
    pid: int = field(default_factory=os.getpid)
    closed: bool = False
    _mutations: Lock = field(default_factory=Lock, repr=False)

    def _request(self, operation: HelperOperation, **configuration: object) -> None:
        with self._mutations:
            self._locked_request(operation, **configuration)

    def _locked_request(self, operation: HelperOperation, **configuration: object) -> None:
        if self.closed or os.getpid() != self.pid:
            raise HelperError("The native lease belongs to another or closed process")
        reply = self.helper.request(
            HelperRequest(
                operation, session=self.session, capability=self.capability, **configuration
            )
        )
        if reply:
            raise HelperError("The helper returned unexpected operation data")

    def configure(
        self,
        *,
        unit: str,
        generation: int,
        vpc_id: str,
        cidrs: tuple[str, ...],
        resolvers: tuple[ResolverEndpoint, ...] = (),
    ) -> None:
        self._request(
            HelperOperation.CONFIGURE,
            unit=unit,
            generation=generation,
            vpc_id=vpc_id,
            cidrs=cidrs,
            resolvers=resolvers,
        )

    def remove(self, *, unit: str, generation: int, keep_dns: bool = False) -> None:
        self._request(HelperOperation.REMOVE, unit=unit, generation=generation, keep_dns=keep_dns)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if os.getpid() == self.pid:
                self._request(HelperOperation.RELEASE)
        finally:
            # EOF remains the cleanup trigger if the release reply was lost.
            self.connection.close()
            self.carrier.close()
            self.capability = b""
            self.closed = True
