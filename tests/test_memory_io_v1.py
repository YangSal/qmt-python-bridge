import ast
import importlib.util
import time
import uuid
from pathlib import Path

import pytest

api = pytest.importorskip('_winapi')


def module():
    assert importlib.util.find_spec('qmt_bridge.memory_io_v1'), 'memory I/O not implemented'
    from qmt_bridge import memory_io_v1
    return memory_io_v1


@pytest.fixture
def pair():
    m = module()
    name = r'\\.\pipe\qmt-test-' + uuid.uuid4().hex
    server = api.CreateNamedPipe(name, 3 | 0x40000000, 4 | 2 | 8, 1, 262144, 262144, 0, 0)
    connection = api.ConnectNamedPipe(server, overlapped=True)
    client = api.CreateFile(name, 0x80000000 | 0x40000000, 0, 0, 3, 0x40000000, 0)
    api.SetNamedPipeHandleState(client, 2, None, None)
    assert connection.GetOverlappedResult(False)[1] == 0
    ends = [m.PipeIO(api, server), m.PipeIO(api, client)]
    yield ends
    for end in ends:
        end.close()
    until = time.monotonic() + 2
    while any(end.closing_pending for end in ends) and time.monotonic() < until:
        for end in ends:
            end.reap_close()
        time.sleep(.001)
    assert not any(end.closing_pending for end in ends)


def test_real_message_pipe_bidirectional_nonblocking(pair):
    left, right = pair
    started = time.monotonic()
    assert left.poll() == []
    assert time.monotonic() - started < .1
    left.send(b'left')
    right.send(b'right')
    got_left, got_right = [], []
    until = time.monotonic() + 2
    while (not got_left or not got_right) and time.monotonic() < until:
        got_left.extend(left.poll())
        got_right.extend(right.poll())
    assert got_left == [b'right'] and got_right == [b'left']


def test_queue_overflow_rejected_without_overwrite(pair):
    left, right = pair
    for n in range(8):
        left.send(str(n).encode())
    with pytest.raises(BufferError):
        left.send(b'ninth')
    received = []
    until = time.monotonic() + 2
    while len(received) < 8 and time.monotonic() < until:
        left.poll()
        received.extend(right.poll())
    assert received == [str(n).encode() for n in range(8)]


def test_oversize_sender_is_rejected_without_native_write(pair):
    left, right = pair
    with pytest.raises(ValueError):
        left.send(b'x' * 262145)
    assert right.poll() == []


def test_close_pending_read_and_peer_disconnect(pair):
    left, right = pair
    assert left.poll() == []
    left.close()
    assert left.closed
    with pytest.raises(RuntimeError):
        left.send(b'after close')
    until = time.monotonic() + 2
    while left.closing_pending and time.monotonic() < until:
        left.reap_close()
    assert not left.closing_pending
    with pytest.raises((EOFError, OSError)):
        right.poll()


def test_embedded_io_is_python36_syntax():
    m = module()
    ast.parse(Path(m.__file__).read_text(), feature_version=(3,6))


@pytest.mark.parametrize('pending_attr',['read_op','write_op'])
def test_delayed_cancel_retains_overlapped_and_handle(pending_attr):
    class Operation:
        completed = False
        def cancel(self):
            pass
        def GetOverlappedResult(self, wait):
            assert wait is False
            return 0, 995 if self.completed else 996
    class Native:
        closed = []
        def CloseHandle(self, handle):
            self.closed.append(handle)
    native = Native()
    channel = module().PipeIO(native, 41)
    op = Operation()
    setattr(channel, pending_attr, op)
    channel.close()
    assert channel.closing_pending and channel.handle == 41
    assert getattr(channel,pending_attr) is op and native.closed == []
    op.completed=True
    assert channel.reap_close() is True
    assert native.closed == [41] and channel.handle is None
