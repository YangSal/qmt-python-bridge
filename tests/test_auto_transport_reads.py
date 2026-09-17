"""Read-only journal retries preserve immutable native request identities."""
import builtins
import errno
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from bigqmt_bridge import QmtDataError
from bigqmt_bridge.auto_transport import AutomaticTransport
from qmt_bridge.protocol import atomic_json, publish_result


@pytest.fixture
def journal(tmp_path):
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1,
                                    'poll_interval': .01})
    request_id = transport.submit('probe', {}, request_id='a' * 32)
    record = json.loads((tmp_path / 'records' / (request_id + '.json')).read_text())
    result = {'auto_protocol': 'auto-kline-v1', 'request_id': request_id,
              'args_hash': record['args_hash'], 'state': 'returned',
              'data': {'synthetic': True}, 'error': None}
    atomic_json(tmp_path / 'states' / (request_id + '.json'), result)
    publish_result(str(tmp_path / 'responses'), request_id, result, None)
    (tmp_path / 'requests' / (request_id + '.json')).unlink()
    return transport, request_id, result


def snapshot(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('directory,suffix', [
    ('records', '.json'), ('states', '.json'),
    ('responses', '.json'), ('responses', '.json.gz'),
])
def test_temporary_access_denial_retries_only_reads_with_original_id(
        journal, monkeypatch, directory, suffix):
    transport, request_id, expected = journal
    before = snapshot(transport.root)
    target = transport.root / directory / (request_id + suffix)
    original_open = builtins.open
    denied = []

    def flaky_open(path, *args, **kwargs):
        if Path(path) == target and len(denied) < 2:
            denied.append(path)
            # CRT open can expose a Windows sharing violation only as errno 13.
            raise PermissionError(errno.EACCES, 'synthetic access denial', str(path))
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', flaky_open)
        assert transport.lookup(request_id) == expected
    assert len(denied) == 2
    assert snapshot(transport.root) == before


@pytest.mark.skipif(os.name != 'nt', reason='real Windows sharing violation')
def test_native_exclusive_handle_is_retried_until_released(journal, monkeypatch):
    import _winapi

    transport, request_id, expected = journal
    target = transport.root / 'states' / (request_id + '.json')
    original_open = builtins.open
    denied = threading.Event()
    released = threading.Event()
    handle = _winapi.CreateFile(str(target), 0x80000000, 0, 0, 3, 0, 0)

    def observe_open(path, *args, **kwargs):
        try:
            return original_open(path, *args, **kwargs)
        except PermissionError:
            if Path(path) == target:
                denied.set()
            raise

    def release():
        try:
            denied.wait(1)
        finally:
            _winapi.CloseHandle(handle)
            released.set()

    thread = threading.Thread(target=release)
    thread.start()
    try:
        monkeypatch.setattr(builtins, 'open', observe_open)
        assert transport.lookup(request_id) == expected
        assert denied.is_set()
    finally:
        denied.set()
        thread.join(2)
        assert released.is_set()


@pytest.mark.parametrize('use_wait', [False, True])
def test_persistent_access_denial_is_bounded_and_not_reported_as_corruption(
        journal, monkeypatch, use_wait):
    import bigqmt_bridge.auto_transport as module

    transport, request_id, _ = journal
    target = transport.root / 'states' / (request_id + '.json')
    original_open = builtins.open
    clock = [10.]
    failures = []

    def inaccessible_open(path, *args, **kwargs):
        if Path(path) == target:
            error = PermissionError(errno.EACCES, 'synthetic access denial', str(path))
            failures.append(error)
            raise error
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', inaccessible_open)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(QmtDataError, match='unavailable') as error:
        if use_wait:
            transport.wait(request_id, timeout=.025)
        else:
            transport.lookup(request_id)
    assert 'corrupt' not in str(error.value)
    assert error.value.__cause__ is failures[-1]
    assert len(failures) > 1
    assert clock[0] - 10 <= (.0250001 if use_wait else .2500001)


@pytest.mark.parametrize('failure', ['json', 'identity', 'gzip', 'io'])
def test_nontransient_journal_errors_are_never_retried(journal, monkeypatch, failure):
    import bigqmt_bridge.auto_transport as module

    transport, request_id, expected = journal
    target = transport.root / 'states' / (request_id + '.json')
    if failure == 'json':
        target.write_text('{invalid JSON')
    elif failure == 'identity':
        atomic_json(target, dict(expected, request_id='b' * 32))
    elif failure == 'gzip':
        payload = transport.root / 'responses' / (request_id + '.json.gz')
        payload.write_bytes(b'not gzip data')
        manifest_path = payload.with_suffix('')
        manifest = json.loads(manifest_path.read_text())
        manifest.update(bytes=13, sha256=hashlib.sha256(b'not gzip data').hexdigest())
        atomic_json(manifest_path, manifest)
    else:
        original_open = builtins.open

        def failed_open(path, *args, **kwargs):
            if Path(path) == target:
                raise OSError(errno.EIO, 'synthetic I/O failure')
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', failed_open)

    def unexpected_sleep(delay):
        pytest.fail('invalid journal or non-permission I/O must not be retried')

    monkeypatch.setattr(module.time, 'sleep', unexpected_sleep)
    with pytest.raises(QmtDataError, match='unavailable' if failure == 'io' else 'corrupt'):
        transport.lookup(request_id)
