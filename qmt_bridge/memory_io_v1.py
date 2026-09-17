"""Bounded overlapped message-pipe I/O usable in restricted QMT Python 3.6."""
from collections import deque


class PipeIO(object):
    def __init__(self, api, handle, max_bytes=262144, max_queue=8):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 262144:
            raise ValueError('invalid pipe frame limit')
        if type(max_queue) is not int or not 1 <= max_queue <= 8:
            raise ValueError('invalid pipe queue limit')
        self.api, self.handle = api, handle
        self.max_bytes, self.max_queue = max_bytes, max_queue
        self.queue = deque()
        self.read_op = None
        self.write_op = None
        self.write_size = 0
        self.closed = False
        self.closing_pending = False
        self._cancelled = False

    @property
    def pending_writes(self):
        return len(self.queue) + (1 if self.write_op is not None else 0)

    def send(self, message):
        if self.closed:
            raise RuntimeError('pipe is closed')
        if not isinstance(message, bytes) or not 0 < len(message) <= self.max_bytes:
            raise ValueError('pipe message exceeds frame limit or is empty')
        if self.pending_writes >= self.max_queue:
            raise BufferError('pipe send queue is full')
        self.queue.append(message)

    def poll(self, limit=8):
        if self.closed:
            self.reap_close()
            raise RuntimeError('pipe is closed')
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('poll batch limit must be 1..32')
        received = []
        try:
            for unused in range(limit):
                progress = False
                if self.write_op is None and self.queue:
                    message = self.queue.popleft()
                    self.write_size = len(message)
                    self.write_op, error = self.api.WriteFile(self.handle, message, overlapped=True)
                if self.write_op is not None:
                    size, error = self.write_op.GetOverlappedResult(False)
                    if error != 996:  # ERROR_IO_INCOMPLETE, never wait in QMT callback.
                        self.write_op = None
                        if error != 0 or size != self.write_size:
                            raise OSError('incomplete/failed pipe write: %s' % error)
                        progress = True
                if self.read_op is None:
                    self.read_op, error = self.api.ReadFile(self.handle, self.max_bytes + 1, overlapped=True)
                size, error = self.read_op.GetOverlappedResult(False)
                if error != 996:
                    operation, self.read_op = self.read_op, None
                    if error != 0 or size > self.max_bytes:
                        raise ValueError('oversized or failed pipe message: %s' % error)
                    if not size:
                        raise EOFError('empty pipe message')
                    message = operation.getbuffer()
                    if not isinstance(message, bytes) or len(message) != size:
                        raise ValueError('pipe read buffer length mismatch')
                    received.append(message)
                    progress = True
                if not progress:
                    break
        except Exception:
            self.close()
            raise
        return received

    def close(self):
        if self.handle is None:
            self.closed = True
            return
        self.closed = True
        self.queue.clear()
        self.closing_pending = True
        if not self._cancelled:
            for operation in (self.read_op, self.write_op):
                if operation is not None:
                    try:
                        operation.cancel()
                    except OSError:
                        pass  # Query completion below before releasing the buffer.
            self._cancelled = True
        self.reap_close()

    def reap_close(self):
        """Keep native OVERLAPPED buffers alive until cancel completion; never wait."""
        if not self.closing_pending:
            return True
        for attr in ('read_op', 'write_op'):
            operation = getattr(self, attr)
            if operation is None:
                continue
            try:
                size, error = operation.GetOverlappedResult(False)
            except OSError as exc:
                # Broken/disconnected pipe or canceled I/O is terminal.
                if getattr(exc, 'winerror', None) not in (6, 109, 232, 233, 995):
                    raise
                error = 995
            if error == 996:
                return False
            setattr(self, attr, None)
        self.api.CloseHandle(self.handle)
        self.handle = None
        self.closing_pending = False
        return True


def connect(api, name):
    """One immediate local connect attempt; caller schedules subsequent attempts."""
    if not isinstance(name, str) or not name.startswith('\\\\.\\pipe\\qmt-memory-'):
        raise ValueError('only local qmt-memory pipe names are allowed')
    handle = api.CreateFile(name, 0x80000000 | 0x40000000, 0, 0, 3,
                            0x40000000 | 0x100000 | 0x10000, 0)
    # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION prevents server impersonation.
    try:
        api.SetNamedPipeHandleState(handle, 2, None, None)
        return PipeIO(api, handle)
    except Exception:
        api.CloseHandle(handle)
        raise
