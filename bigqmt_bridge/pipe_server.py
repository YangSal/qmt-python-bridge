"""Single-connection, local-only overlapped Windows named-pipe listener."""

from __future__ import annotations

import copy
import os

from .pipe_security import PipeSecurity, verify_pipe_dacl


class Server:
    """Accept once, transferring the connected handle to a memory PipeIO.

    No requests or market payloads are written to disk. Call close() explicitly;
    after acceptance the returned PipeIO must be closed separately. A listener
    cancellation is bounded to one second and retains its resources for a later
    close() retry if the operating system has not acknowledged cancellation.
    """

    def __init__(self, name):
        prefix = "\\\\.\\pipe\\"
        if (not isinstance(name, str) or not name.startswith(prefix)
                or len(name) <= len(prefix) or len(name) > 256
                or "\x00" in name or "\\" in name[len(prefix):]
                or "/" in name[len(prefix):]):
            raise ValueError("a local \\\\.\\pipe\\name with no nested path is required")
        if os.name != "nt":
            raise OSError("Windows named pipes are required")
        import _winapi

        self._api = _winapi
        self._handle = None
        self._connect = None
        self._closing = False
        self._closed = False
        security = PipeSecurity()
        try:
            # DUPLEX includes GENERIC_READ (and its READ_CONTROL permission).
            # OVERLAPPED | FIRST_PIPE_INSTANCE; READ_CONTROL is not an open flag.
            self._handle = _winapi.CreateNamedPipe(
                name, 0x00000003 | 0x40000000 | 0x00080000,
                # TYPE_MESSAGE | READMODE_MESSAGE | REJECT_REMOTE_CLIENTS.
                0x00000004 | 0x00000002 | 0x00000008,
                1, 262144, 262144, 0, security.address,
            )
            self._security = verify_pipe_dacl(self._handle)
            self._security["remote_clients_rejected"] = True
            self._connect = _winapi.ConnectNamedPipe(self._handle, overlapped=True)
        except BaseException:
            if self._handle is not None:
                _winapi.CloseHandle(self._handle)
                self._handle = None
            raise
        finally:
            security.close()

    def accept_nonblocking(self):
        if self._closing or self._closed or self._handle is None:
            return None
        _, error = self._connect.GetOverlappedResult(False)
        if error == 996:  # ERROR_IO_INCOMPLETE
            return None
        if error:
            raise OSError(error, "pipe connection failed")
        from qmt_bridge.memory_io_v1 import PipeIO

        channel = PipeIO(self._api, self._handle)
        self._handle = None
        self._connect = None
        return channel

    def security_report(self):
        """Return a copy of the verified creation-time security policy."""
        return copy.deepcopy(self._security)

    def close(self):
        if self._closed:
            return
        self._closing = True
        if self._connect is not None:
            self._connect.cancel()
            if self._api.WaitForSingleObject(self._connect.event, 1000) != 0:
                raise TimeoutError("pipe connection cancellation incomplete; retry close")
            # Keep the operation and handle alive until the kernel completes it.
            self._connect.GetOverlappedResult(False)
            self._connect = None
        if self._handle is not None:
            self._api.CloseHandle(self._handle)
            self._handle = None
        self._closed = True
