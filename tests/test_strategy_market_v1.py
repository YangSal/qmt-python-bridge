"""Independent market scheduler bounds and restricted bootstrap, without QMT UI."""
import ast
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / 'qmt_bridge/strategy_market_v1.py'


def strategy():
    assert STRATEGY.exists(), 'isolated market strategy missing'
    return runpy.run_path(str(STRATEGY))


def install_worker(namespace, poll, clock):
    namespace['bridge_market_poll'].__globals__.update(
        _worker=SimpleNamespace(poll=poll), time=SimpleNamespace(perf_counter=clock))


def test_strategy_supports_embedded_python36_syntax():
    assert STRATEGY.exists(), 'isolated market strategy missing'
    ast.parse(STRATEGY.read_text('ascii'), feature_version=(3, 6))


def test_callback_processes_at_most_eight_ready_requests():
    ns, calls = strategy(), []
    def poll():
        calls.append(True)
        return True
    install_worker(ns, poll, lambda: 0.)
    ns['bridge_market_poll'](None)
    assert len(calls) == 8


def test_callback_stops_starting_work_after_twenty_milliseconds():
    ns, clock, calls = strategy(), [0.], []
    def poll():
        calls.append(True)
        clock[0] += .011
        return True
    install_worker(ns, poll, lambda: clock[0])
    ns['bridge_market_poll'](None)
    assert len(calls) == 2
    assert ns['bridge_market_poll'].__globals__['_metrics']['over_budget_callbacks'] == 1


def test_callback_returns_when_queue_is_empty():
    ns, calls = strategy(), []
    def poll():
        calls.append(True)
        return False
    install_worker(ns, poll, lambda: 0.)
    ns['bridge_market_poll'](None)
    assert len(calls) == 1


def test_reentrant_callback_does_not_start_another_worker_poll():
    ns, calls = strategy(), []
    def poll():
        calls.append(True)
        ns['bridge_market_poll'](None)
        return False
    install_worker(ns, poll, lambda: 0.)
    ns['bridge_market_poll'](None)
    assert len(calls) == 1


def test_failed_timer_registration_releases_worker_lock(tmp_path):
    ns = strategy()
    ns['init'].__globals__['BRIDGE_DIR'] = str(tmp_path)
    class Context:
        def run_time(self, *args):
            raise RuntimeError('timer unsupported')
    with pytest.raises(RuntimeError, match='timer unsupported'):
        ns['init'](Context())
    assert ns['init'].__globals__['_worker'] is None
    from qmt_bridge.auto_worker import AutomaticWorker
    worker = AutomaticWorker(str(tmp_path), object(), {})
    worker.close()


def test_restricted_bootstrap_real_worker_probe_and_downloads_disabled(tmp_path):
    assert STRATEGY.exists(), 'isolated market strategy missing'
    result = subprocess.run([sys.executable, '-I', '-S', '-c', r'''
import builtins
import sys
source_root, runtime_root = sys.argv[1:]
sys.path.insert(0, source_root)
original_import = builtins.__import__
def reduced_import(name, *args, **kwargs):
    if name.split('.')[0] in ('importlib', 'ctypes', 'socket', 'threading'):
        raise ModuleNotFoundError('unavailable embedded module', name=name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = reduced_import
with open(source_root + '/qmt_bridge/strategy_market_v1.py', encoding='ascii') as stream:
    code = compile(stream.read(), 'strategy_market_v1.py', 'exec')
namespace = {'__name__': 'qmt_market_test'}
exec(code, namespace)
namespace['BRIDGE_DIR'] = runtime_root
class Context:
    def run_time(self, *args):
        self.timer = args
context = Context()
namespace['init'](context)
try:
    assert context.timer == ('bridge_market_poll', '10nMilliSecond', '2020-01-01 00:00:00')
    from bigqmt_bridge.auto_transport import AutomaticTransport
    transport = AutomaticTransport({'bridge_dir': runtime_root, 'timeout': 2})
    request = transport.submit('probe', {})
    namespace['bridge_market_poll'](context)
    result = transport.lookup(request)
    assert result['state'] == 'returned' and result['data']['downloads_enabled'] is False
    request = transport.submit('download_kline', {'stock_code': '000001.SZ', 'period': '1d', 'date': '20260813'})
    namespace['bridge_market_poll'](context)
    assert transport.lookup(request)['state'] == 'failed'
finally:
    namespace['stop'](context)
assert namespace['_worker'] is None
''', str(ROOT), str(tmp_path), ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
