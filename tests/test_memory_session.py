import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip('_winapi')


@pytest.fixture
def tmp_path():
    # Authentication files require trusted ancestors; machine-wide Temp may be shared.
    with tempfile.TemporaryDirectory(prefix='qmt-memory-test-', dir=Path.home()) as location:
        yield Path(location)


def module():
    assert importlib.util.find_spec('bigqmt_bridge.memory_session'), 'session harness missing'
    from bigqmt_bridge import memory_session
    return memory_session


def test_private_bundle_has_immutable_python36_worker_and_no_secret_output(tmp_path, capsys):
    m = module()
    config = m.prepare(tmp_path / 'session')
    value = json.loads(config.read_text())
    assert len(value['auth_key']) == 64
    assert value['auth_key'] not in capsys.readouterr().out
    assert value['auth_key'] not in Path(value['strategy']).read_text()
    ast.parse(Path(value['strategy']).read_text(), feature_version=(3, 6))
    for path in (config.parent / value['package']).glob('*.py'):
        ast.parse(path.read_text(), feature_version=(3, 6))
    with pytest.raises(FileExistsError):
        m.prepare(config.parent)


def test_cross_process_qualification_checks_ten_thousand_and_leaves_restart_incomplete(tmp_path):
    m = module()
    config = m.prepare(tmp_path / 'session')
    code = '''
import importlib,json,sys,time,_winapi
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text());sys.path.insert(0,str(Path(sys.argv[1]).parent))
w=importlib.import_module(c['package']+'.memory_client_v1').MemoryWorker(_winapi,c['pipe'],c['run_id'],c['auth_key'])
try:
 end=time.monotonic()+30
 while time.monotonic()<end:
  w.poll();time.sleep(.002)
finally:
 w.close()
 for _ in range(100):
  w.reap();time.sleep(.001)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        report = m.qualify(config, timeout=5, idle_samples=5)
        assert report['status'] == 'incomplete'
        assert report['gates']['synthetic']['verified_samples'] == 10000
        assert report['gates']['restart']['status'] == 'incomplete'
        assert report['gates']['faults']['passed'] == ['checksum', 'truncated', 'protocol', 'old_session', 'duplicate_seq', 'oversized']
        assert report['gates']['queue']['overflow_rejected'] is True
        assert report['gates']['transport']['status'] == 'passed'
        assert report['peer']['python'].startswith('3.10')
        assert json.loads(config.read_text())['auth_key'] not in json.dumps(report)
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_missing_peer_is_bounded_and_never_verified(tmp_path):
    m = module()
    config = m.prepare(tmp_path / 'session')
    result = m.qualify(config, timeout=.05, idle_samples=1)
    assert result['status'] == 'incomplete'
    assert result['failure'] == {'stage': 'connection', 'error': 'TimeoutError'}


def test_whole_probe_deadline_and_parameter_limits(tmp_path):
    import time
    m = module()
    config = m.prepare(tmp_path / 'session')
    started = time.monotonic()
    result = m.qualify(config, timeout=1, max_seconds=.03)
    assert time.monotonic() - started < .5
    assert result['status'] == 'incomplete'
    with pytest.raises(ValueError):
        m.qualify(config, timeout=31)
    with pytest.raises(ValueError):
        m.qualify(config, max_seconds=301)


def test_pending_listener_cancellation_keeps_strong_owner():
    import weakref
    import gc
    m = module()
    class DelayedListener:
        pending = True
        def close(self):
            if self.pending:
                raise TimeoutError('still pending')
    listener = DelayedListener()
    reference = weakref.ref(listener)
    with pytest.raises(TimeoutError):
        m.close_listener(listener)
    del listener
    gc.collect()
    assert reference() is not None
    reference().pending = False
    m.close_listener(reference())
    gc.collect()
    assert reference() is None


def test_restart_requires_complete_baseline_and_user_cleanup_evidence(tmp_path):
    m = module()
    config = m.prepare(tmp_path / 'session')
    assert m.restart_check(config, {'status': 'incomplete', 'gates': {}}, True)['status'] == 'incomplete'
    assert m.restart_check(config, {}, False)['failure']['stage'] == 'restart_prerequisites'


def test_market_bundle_requires_accepted_unchanged_core(tmp_path):
    import hashlib
    m = module()
    with pytest.raises(ValueError):
        m.prepare(tmp_path/'refused', qualification={})
    assert not (tmp_path/'refused').exists()
    core = Path(m.__file__).resolve().parents[1]/'qmt_bridge'
    report = {'status': 'named_pipe_verified', 'peer': {'python': '3.6.8'},
        'core_sha256': {name: hashlib.sha256((core/name).read_bytes()).hexdigest() for name in
            ('memory_client_v1.py', 'memory_protocol_v1.py', 'memory_io_v1.py')}}
    config = m.prepare(tmp_path/'market', qualification=report)
    settings = json.loads(config.read_text())
    assert settings['mode'] == 'market'
    strategy = Path(settings['strategy']).read_text()
    assert 'MarketMemoryWorker' in strategy
    assert 'QMT market memory v1 ready' in strategy
    assert settings['auth_key'] not in strategy
    ast.parse(strategy, feature_version=(3, 6))
    report['core_sha256']['memory_io_v1.py'] = 'wrong'
    with pytest.raises(ValueError):
        m.prepare(tmp_path/'stale', qualification=report)


def test_probe_cli_persists_failure_and_returns_nonzero(tmp_path):
    m = module()
    config = m.prepare(tmp_path/'cli')
    report = tmp_path/'new-evidence'/'failure.json'
    result = subprocess.run([sys.executable, '-m', 'bigqmt_bridge.memory_session', 'qualify',
        '--config', str(config), '--report', str(report), '--timeout', '.05'],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    saved = json.loads(report.read_text())
    assert saved['failure'] == {'stage': 'connection', 'error': 'TimeoutError'}
    assert saved['status'] == 'incomplete'


def test_stop_watch_does_not_mistake_local_timeout_for_peer_close(monkeypatch):
    m = module()
    monkeypatch.setattr(m, 'load_config', lambda path: {})
    class TimedOut:
        def __init__(self, *args):
            pass
        def open(self):
            return self
        def call(self, kind, **kwargs):
            if kind == 'metrics':
                return {'instance': 'same'}
            raise TimeoutError('local deadline, peer still connected')
        def close(self):
            pass
    monkeypatch.setattr(m, 'Connection', TimedOut)
    report = m.watch_disconnect('unused', {'stage': 'restart_pending', 'instance': 'same', 'gates': {}})
    assert report['failure']['error'] == 'TimeoutError'
    assert report['gates'].get('disconnect', {}).get('status') != 'passed'


def test_restart_rejects_superseded_stop_observer_evidence(monkeypatch):
    m = module()
    calls = []
    monkeypatch.setattr(m, 'load_config', lambda path: calls.append(path))
    gates = {name: {'status':'passed'} for name in ('transport','authentication','synthetic',
        'boundary','queue','scheduling','faults','cleanup','isolation','disconnect')}
    gates['synthetic']['verified_samples'] = 10000
    result = m.restart_check('unused', {'gates': gates}, True)
    assert result['status'] == 'incomplete'
    assert calls == []


def test_unified_bundle_contains_one_timer_and_unchanged_history_sources(tmp_path):
    import hashlib
    m = module()
    source = Path(m.__file__).resolve().parents[1]/'qmt_bridge'
    accepted = {'status': 'named_pipe_verified', 'peer': {'python': '3.6.8'},
        'core_sha256': {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in
            ('memory_client_v1.py','memory_protocol_v1.py','memory_io_v1.py')}}
    with pytest.raises(ValueError):
        m.prepare(tmp_path/'no-gate', history_root=str(tmp_path/'history'))
    config = m.prepare(tmp_path/'unified', qualification=accepted,
                       history_root=str(tmp_path/'history'), downloads_enabled=True)
    settings = json.loads(config.read_text())
    assert settings['mode'] == 'market' and settings['service_mode'] == 'unified'
    assert settings['downloads_enabled'] is True
    strategy = Path(settings['strategy']).read_text()
    assert strategy.count('C.run_time(') == 1
    assert 'QMT unified collector v1 ready' in strategy
    assert 'UnifiedWorker' in strategy
    ast.parse(strategy, feature_version=(3, 6))
    for name in ('auto_worker.py','auto_protocol.py','worker.py','protocol.py'):
        assert (config.parent/settings['package']/name).read_bytes() == (source/name).read_bytes()


def test_generated_unified_timer_failure_releases_history_lock(tmp_path):
    import hashlib
    import runpy
    m = module()
    source = Path(m.__file__).resolve().parents[1]/'qmt_bridge'
    accepted = {'status':'named_pipe_verified','peer':{'python':'3.6.8'},
        'core_sha256': {name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in
            ('memory_client_v1.py','memory_protocol_v1.py','memory_io_v1.py')}}
    history = tmp_path/'history'
    path = m.prepare(tmp_path/'unified', qualification=accepted, history_root=str(history))
    settings = json.loads(path.read_text())
    class Context:
        def run_time(self, *args):
            raise RuntimeError('unsupported timer')
    namespace = runpy.run_path(settings['strategy'])
    with pytest.raises(RuntimeError):
        namespace['init'](Context())
    from qmt_bridge.auto_worker import AutomaticWorker
    worker = AutomaticWorker(str(history), object(), {})
    worker.close()
