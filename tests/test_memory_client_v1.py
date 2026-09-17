import ast
import importlib.util
import json
import threading
import time
import uuid
from pathlib import Path

import pytest

api = pytest.importorskip('_winapi')


def module():
    assert importlib.util.find_spec('qmt_bridge.memory_client_v1'), 'embedded memory client missing'
    from qmt_bridge import memory_client_v1
    return memory_client_v1


def test_embedded_worker_qualifies_without_qmt_calls():
    m = module()
    from bigqmt_bridge.pipe_server import Server
    from qmt_bridge.memory_protocol_v1 import encode, decode, auth_proof
    run = uuid.uuid4().hex
    name = r'\\.\pipe\qmt-memory-' + run
    server = Server(name)
    key = 'd'*64
    worker = m.MemoryWorker(api, name, run, key)
    channel = None
    def exchange(frame):
        channel.send(frame)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            worker.poll()
            frames = channel.poll()
            if frames:
                assert len(frames) == 1
                return decode(frames[0])
        pytest.fail('embedded worker response timed out')
    try:
        worker.poll()
        channel = server.accept_nonblocking()
        session = uuid.uuid4().hex
        hello = exchange(encode(run, 1, 'hello', {'session': session, 'challenge': 'b'*32,
            'proof':auth_proof(key,'server-hello',run,session,'b'*32)}))
        assert hello['body']['echo'] == 'b'*32
        assert exchange(encode(session, 2, 'confirm', {'echo': hello['body']['challenge'],
            'proof':auth_proof(key,'server-confirm',run,session,'b'*32,hello['body']['challenge'])}))['kind'] == 'ready'
        batch = [[n, 'payload-%d' % n] for n in range(256)]
        assert exchange(encode(session, 3, 'echo', {'samples': batch}))['body']['samples'] == batch
        metrics = exchange(encode(session, 4, 'metrics', {}))['body']
        assert metrics['qmt_calls'] == 0 and metrics['connections'] == 1
        capacity = exchange(encode(session, 5, 'queue_test', {}))['body']
        assert capacity == {'accepted': 8, 'overflow_rejected': True, 'remaining': 0}
        for seq in range(6,14):
            channel.send(encode(session,seq,'ping',{}))
        replies=[]
        until=time.monotonic()+2
        while len(replies)<8 and time.monotonic()<until:
            replies.extend(decode(frame) for frame in channel.poll())
            worker.poll()
        assert [reply['seq'] for reply in replies] == list(range(6,14))
        assert exchange(encode(session, 14, 'bye', {}))['kind'] == 'bye_ack'
    finally:
        worker.close()
        if channel:
            channel.close()
            until=time.monotonic()+2
            while channel.closing_pending and time.monotonic()<until:
                channel.reap_close()
        server.close()
        for _ in range(10):
            worker.reap()
        assert worker.resources_closed


def test_qmt_modules_remain_python36_syntax():
    m = module()
    ast.parse(Path(m.__file__).read_text(), feature_version=(3, 6))


def test_frame_failure_does_not_log_or_write_payload(capsys):
    m = module()
    from bigqmt_bridge.pipe_server import Server
    run = uuid.uuid4().hex
    server = Server(r'\\.\pipe\qmt-memory-' + run)
    worker = m.MemoryWorker(api, r'\\.\pipe\qmt-memory-' + run, run, 'd'*64)
    worker.poll()
    channel = server.accept_nonblocking()
    channel.send(b'PRIVATE MARKET CONTENT')
    until=time.monotonic()+1
    while not worker.rejections and time.monotonic()<until:
        channel.poll()
        worker.poll()
    assert worker.rejections == 1
    assert 'PRIVATE' not in capsys.readouterr().out
    worker.close()
    channel.close()
    server.close()
    while channel.closing_pending:
        channel.reap_close()
    worker.reap()
