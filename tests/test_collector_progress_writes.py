"""Consumer progress publication survives brief Windows file sharing conflicts."""
import importlib
import json
import sys
import threading

import pytest

from qmt_bridge import protocol


@pytest.mark.parametrize('module_name', ['collector', 'market_collector', 'realtime_collector'])
def test_consumer_retries_progress_replacement_without_losing_old_json(tmp_path, monkeypatch, module_name):
    module = importlib.import_module('bigqmt_bridge.' + module_name)
    target = tmp_path / 'summary.json'
    target.write_text('{"completed":1}')
    replace = protocol.os.replace
    attempts = []

    def busy_then_release(source, destination):
        attempts.append(destination)
        if len(attempts) <= 2:
            assert json.loads(target.read_text()) == {'completed': 1}
            raise PermissionError(5, 'access denied while replacing progress')
        return replace(source, destination)

    monkeypatch.setattr(protocol.os, 'replace', busy_then_release)
    module.atomic_json(target, {'completed': 2})
    assert json.loads(target.read_text()) == {'completed': 2}
    assert len(attempts) == 3
    assert list(tmp_path.glob('*.tmp')) == []


def test_permanent_denial_is_bounded_and_preserves_previous_progress(tmp_path, monkeypatch):
    from bigqmt_bridge import archive, market_collector
    clock, attempts = [0.], []
    monkeypatch.setattr(archive.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(archive.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    target = tmp_path / 'summary.json'
    target.write_text('{"completed":1}')
    def denied(*args):
        attempts.append(1)
        raise PermissionError(5, 'permanent denial')
    monkeypatch.setattr(protocol.os, 'replace', denied)
    with pytest.raises(PermissionError):
        market_collector.atomic_json(target, {'completed': 2})
    assert 1 < len(attempts) <= 52
    assert .49 <= clock[0] <= .501
    assert json.loads(target.read_text()) == {'completed': 1}
    assert list(tmp_path.glob('*.tmp')) == []


def test_non_permission_failure_is_not_retried(tmp_path, monkeypatch):
    from bigqmt_bridge import market_collector
    attempts = []
    def disk_full(*args):
        attempts.append(1)
        raise OSError(28, 'disk full')
    monkeypatch.setattr(protocol.os, 'replace', disk_full)
    with pytest.raises(OSError):
        market_collector.atomic_json(tmp_path / 'summary.json', {})
    assert len(attempts) == 1


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows file sharing semantics')
def test_actual_windows_reader_without_delete_sharing(tmp_path):
    import _winapi
    from bigqmt_bridge import market_collector
    target = tmp_path / 'summary.json'
    target.write_text('{"completed":1}')
    # A real external-style reader permits reading/writing, but not replacement.
    handle = _winapi.CreateFile(str(target), 0x80000000, 3, 0, 3, 0, 0)
    try:
        with pytest.raises(PermissionError):
            protocol.atomic_json(target, {'completed': 2})
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    release = threading.Timer(.05, _winapi.CloseHandle, args=(handle,))
    release.start()
    try:
        market_collector.atomic_json(target, {'completed': 3})
    finally:
        release.join(timeout=1)
    assert not release.is_alive()
    assert json.loads(target.read_text()) == {'completed': 3}
    assert list(tmp_path.glob('*.tmp')) == []
