import ast
import importlib.util
from pathlib import Path

import pytest


def module():
    assert importlib.util.find_spec('qmt_bridge.unified_worker_v1'), 'single QMT service missing'
    from qmt_bridge import unified_worker_v1
    return unified_worker_v1


def fake_worker(monkeypatch, *, ready=True, cost=0):
    m = module()
    calls, clock = [], [0.]
    class History:
        def __init__(self, root, context, namespace, downloads_enabled):
            self.enabled = downloads_enabled
        def poll(self):
            calls.append('history'); clock[0] += cost
            return ready
        def close(self):
            calls.append('history_close')
    monkeypatch.setattr(m, 'AutomaticWorker', History)
    monkeypatch.setattr(m.MarketMemoryWorker, 'poll', lambda self: calls.append('memory'))
    monkeypatch.setattr(m.time, 'perf_counter', lambda: clock[0])
    worker = m.UnifiedWorker(None, r'\\.\pipe\qmt-memory-test', 'a'*32, 'b'*64,
                            object(), 'test-history', {}, downloads_enabled=True)
    return m, worker, calls


def test_unified_service_prioritizes_memory_and_bounds_history(monkeypatch):
    m, worker, calls = fake_worker(monkeypatch)
    worker.poll()
    assert calls[0] == 'memory'
    assert calls.count('history') == 8
    assert calls.count('memory') == 9
    assert worker.history.enabled
    worker.close()
    assert worker.resources_closed


def test_native_overrun_starts_no_additional_history(monkeypatch):
    m, worker, calls = fake_worker(monkeypatch, cost=.03)
    worker.poll()
    assert calls == ['memory', 'history', 'memory']
    assert worker._application('service_status', {})[1]['history_overruns'] == 1
    worker.close()


def test_memory_client_absence_does_not_stop_history(monkeypatch):
    m, worker, calls = fake_worker(monkeypatch, ready=False)
    worker.poll()
    assert calls == ['memory', 'history', 'memory']
    worker.close()
    calls.clear()
    worker.poll()
    assert 'history' not in calls


def test_history_errors_are_bounded_and_visible(monkeypatch):
    m, worker, calls = fake_worker(monkeypatch)
    def failed():
        calls.append('history_fail')
        raise RuntimeError('PRIVATE_NATIVE_DATA')
    worker.history.poll = failed
    for _ in range(5):
        worker.poll()
    status = worker._application('service_status', {})[1]
    assert calls.count('history_fail') == 3
    assert status['history_suspended'] is True
    assert status['history_error'] == 'RuntimeError'
    assert 'PRIVATE' not in str(status)
    worker.close()


def test_real_history_lock_is_released_on_close(tmp_path):
    m = module()
    from qmt_bridge.auto_worker import AutomaticWorker
    worker = m.UnifiedWorker(None, r'\\.\pipe\qmt-memory-test', 'a'*32, 'b'*64,
                            object(), str(tmp_path), {})
    assert worker.history.downloads_enabled is False
    worker.close()
    replacement = AutomaticWorker(str(tmp_path), object(), {})
    replacement.close()
    ast.parse(Path(m.__file__).read_text(), feature_version=(3, 6))
