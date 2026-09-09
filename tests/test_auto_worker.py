import ast
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _download_args(date='20200102', period='1d'):
    return {'stock_code': '000001.SZ', 'period': period, 'date': date}


def _wait_for_request(root):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        requests = list((root / 'requests').glob('*.json'))
        if requests:
            return requests[0]
        time.sleep(.005)
    raise AssertionError('request was not published')


def test_successful_real_file_roundtrip_returns_data_only(tmp_path):
    from bigqmt_bridge.auto_transport import AutomaticTransport
    from qmt_bridge.auto_worker import AutomaticWorker

    calls = []

    def download(stock_code, period, start_time, end_time):
        calls.append((stock_code, period, start_time, end_time))
        return None

    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': download}, downloads_enabled=True)
    transport = AutomaticTransport({
        'bridge_dir': str(tmp_path), 'timeout': 2, 'poll_interval': .005
    })
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(transport.call, 'download_kline', _download_args())
            _wait_for_request(tmp_path)
            assert worker.poll()
            data = pending.result()
        assert data['api'] == 'download_history_data'
        assert data['data_ready'] is False
        assert data['return_value'] is None
        assert data['elapsed_seconds'] >= 0
        assert calls == [('000001.SZ', '1d', '20200102', '20200102')]
    finally:
        worker.close()


def test_same_request_id_is_published_once_and_different_args_are_rejected(tmp_path):
    from bigqmt_bridge import QmtDataError
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = 'a' * 32
    assert transport.submit('download_kline', _download_args(), rid) == rid
    assert transport.submit('download_kline', _download_args(), rid) == rid
    assert len(list((tmp_path / 'requests').glob('*.json'))) == 1
    with pytest.raises(QmtDataError):
        transport.submit('download_kline', _download_args(period='5m'), rid)
    assert len(list((tmp_path / 'requests').glob('*.json'))) == 1


def test_queue_is_bounded_to_32_unfinished_requests(tmp_path):
    from bigqmt_bridge import QmtDataError
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    for number in range(32):
        transport.submit('probe', {'number': number}, '%032x' % number)
    with pytest.raises(QmtDataError, match='32'):
        transport.submit('probe', {'number': 32}, '%032x' % 32)
    assert len(list((tmp_path / 'requests').glob('*.json'))) == 32
    assert not (tmp_path / 'records' / ('%032x' % 32 + '.json')).exists()


def test_disabled_download_never_reaches_api(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    calls = []
    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': lambda *args: calls.append(args)})
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': .1})
    rid = transport.submit('download_kline', _download_args())
    try:
        worker.poll()
        assert transport.lookup(rid)['state'] == 'failed'
        assert calls == []
    finally:
        worker.close()


def test_missing_global_download_api_fails_without_native_fallback(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    class Context:
        def download_history_data(self, *args):
            raise AssertionError('context/native fallback must not be called')

    worker = AutomaticWorker(str(tmp_path), Context(), {}, downloads_enabled=True)
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('download_kline', _download_args())
    try:
        assert worker.poll()
        result = transport.lookup(rid)
        assert result['state'] == 'failed'
        assert 'unavailable' in result['error']
    finally:
        worker.close()


def test_interrupted_running_request_becomes_unknown_and_is_never_replayed(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    calls = []
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('download_kline', _download_args())
    request = tmp_path / 'requests' / (rid + '.json')
    request.replace(tmp_path / 'running' / request.name)
    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': lambda *args: calls.append(args)},
                             downloads_enabled=True)
    try:
        result = transport.lookup(rid)
        assert result['state'] == 'unknown'
        assert calls == []
        assert not list((tmp_path / 'requests').glob('*.json'))
        assert not list((tmp_path / 'running').glob('*.json'))
        assert worker.poll() is False
    finally:
        worker.close()


def test_completed_response_is_returned_after_worker_restart(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('probe', {})
    first = AutomaticWorker(str(tmp_path), object(), {})
    try:
        assert first.poll()
        before = transport.lookup(rid)
    finally:
        first.close()
    second = AutomaticWorker(str(tmp_path), object(), {})
    try:
        after = transport.lookup(rid)
        assert before['state'] == after['state'] == 'returned'
        assert after['data']['automatic_kline'] == 'auto-kline-v1'
        assert after['data']['downloads_enabled'] is False
        assert after['data']['worker_version'] == 3
        assert second.poll() is False
    finally:
        second.close()


def test_immutable_record_without_queued_request_is_unknown_not_republished(tmp_path):
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('probe', {'scope': 'one'}, 'b' * 32)
    (tmp_path / 'requests' / (rid + '.json')).unlink()
    assert transport.lookup(rid)['state'] == 'unknown'
    assert transport.submit('probe', {'scope': 'one'}, rid) == rid
    assert not (tmp_path / 'requests' / (rid + '.json')).exists()


def test_native_late_field_and_late_completion_remain_queryable(tmp_path, monkeypatch):
    import qmt_bridge.auto_worker as auto_worker
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    def slow_download(*args):
        return {'late': 'native-value'}

    transport = AutomaticTransport({
        'bridge_dir': str(tmp_path), 'timeout': 1, 'poll_interval': .001
    })
    rid = transport.submit('download_kline', _download_args())
    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': slow_download}, downloads_enabled=True)
    try:
        request_path = tmp_path / 'requests' / (rid + '.json')
        request = json.loads(request_path.read_text())
        request['deadline'] = 100.5
        request_path.write_text(json.dumps(request), encoding='utf-8')
        first_time = [100.0]
        monkeypatch.setattr(auto_worker.time, 'time',
                            lambda: first_time.pop() if first_time else 101.0)
        assert worker.poll()
        result = transport.lookup(rid)
        assert result['state'] == 'returned'
        assert result['late'] is True
        assert result['data']['return_value']['late'] == 'native-value'
    finally:
        worker.close()


def test_negative_or_false_download_return_is_failed(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    for number, return_value in enumerate((False, -1)):
        root = tmp_path / str(number)
        transport = AutomaticTransport({'bridge_dir': str(root), 'timeout': 1})
        worker = AutomaticWorker(str(root), object(),
                                 {'down_history_data': lambda *args, value=return_value: value},
                                 downloads_enabled=True)
        try:
            rid = transport.submit('download_kline', _download_args())
            assert worker.poll()
            assert transport.lookup(rid)['state'] == 'failed'
        finally:
            worker.close()


def test_lookup_pending_wait_timeout_and_call_errors_keep_identity(tmp_path):
    from bigqmt_bridge import QmtDataError
    from bigqmt_bridge.auto_transport import AutomaticTransport, QmtRequestTimeout

    transport = AutomaticTransport({
        'bridge_dir': str(tmp_path), 'timeout': .02, 'poll_interval': .001
    })
    rid = transport.submit('probe', {})
    assert transport.lookup(rid)['state'] == 'pending'
    with pytest.raises(QmtRequestTimeout) as caught:
        transport.wait(rid)
    assert caught.value.request_id == rid
    assert len(list((tmp_path / 'requests').glob('*.json'))) == 1
    with pytest.raises(QmtDataError):
        transport.call('not_allowed', {})


def test_corrupt_server_state_fails_closed_without_reexecution(tmp_path):
    from bigqmt_bridge import QmtDataError
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    calls = []
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('download_kline', _download_args())
    (tmp_path / 'states' / (rid + '.json')).write_text('{broken', encoding='utf-8')
    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': lambda *args: calls.append(args)},
                             downloads_enabled=True)
    try:
        assert worker.poll()
        with pytest.raises(QmtDataError):
            transport.lookup(rid)
        assert calls == []
    finally:
        worker.close()


def test_second_automatic_worker_is_excluded_by_os_lock(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker

    first = AutomaticWorker(str(tmp_path), object(), {})
    try:
        with pytest.raises((OSError, RuntimeError)):
            AutomaticWorker(str(tmp_path), object(), {})
    finally:
        first.close()


def test_new_qmt_files_parse_as_python36():
    for name in ('auto_protocol.py', 'auto_worker.py', 'strategy_auto.py'):
        ast.parse((Path('qmt_bridge') / name).read_text(encoding='utf-8'),
                  feature_version=(3, 6))


def test_strategy_auto_lifecycle_uses_independent_root_and_one_second_timer(tmp_path, monkeypatch):
    from qmt_bridge import strategy_auto

    events = []

    class FakeWorker:
        def __init__(self, root, context, api, downloads_enabled=False):
            self.root = root
            self.downloads_enabled = downloads_enabled
            self.closed = False

        def close(self):
            self.closed = True

        def poll(self):
            events.append('poll')

    class Context:
        def run_time(self, *args):
            events.append(args)

    monkeypatch.setattr(strategy_auto, 'BRIDGE_DIR', str(tmp_path))
    monkeypatch.setattr(strategy_auto, 'AutomaticWorker', FakeWorker, raising=False)
    context = Context()
    try:
        strategy_auto.init(context)
        first = strategy_auto._worker
        strategy_auto.init(context)
        assert first.closed is True
        assert strategy_auto._worker.downloads_enabled is False
        assert events[-1] == ('bridge_poll', '1nSecond', '2020-01-01 00:00:00')
        strategy_auto.bridge_poll(context)
        assert events[-1] == 'poll'
        strategy_auto.handlebar(context)
    finally:
        strategy_auto.stop(context)
    assert strategy_auto._worker is None


def test_request_record_is_published_before_queue_file(tmp_path, monkeypatch):
    from bigqmt_bridge import auto_transport

    observed = []
    real_atomic_json = auto_transport.atomic_json

    def recording_atomic_json(path, value):
        observed.append(Path(path).parent.name)
        return real_atomic_json(path, value)

    monkeypatch.setattr(auto_transport, 'atomic_json', recording_atomic_json)
    transport = auto_transport.AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    transport.submit('probe', {})
    assert observed[:2] == ['records', 'requests']


def test_record_and_envelope_bind_request_identity(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('probe', {})
    record = json.loads((tmp_path / 'records' / (rid + '.json')).read_text())
    worker = AutomaticWorker(str(tmp_path), object(), {})
    try:
        assert worker.poll()
        result = transport.lookup(rid)
        assert result['request_id'] == rid
        assert result['args_hash'] == record['args_hash']
        assert result['data'] is not None
        assert result['error'] is None
    finally:
        worker.close()


@pytest.mark.parametrize('key', ['timeout', 'poll_interval'])
def test_nonfinite_transport_timing_is_rejected(tmp_path, key):
    from bigqmt_bridge.auto_transport import AutomaticTransport

    config = {'bridge_dir': str(tmp_path), 'timeout': 1, 'poll_interval': .1}
    config[key] = float('nan')
    with pytest.raises(ValueError):
        AutomaticTransport(config)


def test_queued_artifact_without_immutable_record_fails_closed(tmp_path):
    from bigqmt_bridge import QmtDataError
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('probe', {})
    (tmp_path / 'records' / (rid + '.json')).unlink()
    with pytest.raises(QmtDataError, match='record'):
        transport.lookup(rid)


def test_corrupt_response_is_not_reexecuted_or_allowed_to_break_poll(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    class Context:
        def get_market_data_ex(self, **kwargs):
            raise AssertionError('corrupt response must not be replayed')

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('market_data', {
        'stock_list': ['000001.SZ'], 'fields': [], 'period': '1d',
        'start_time': '20200102', 'end_time': '20200102',
        'subscribe': False, 'fill_data': False,
    })
    (tmp_path / 'responses' / (rid + '.json')).write_text('{broken', encoding='utf-8')
    worker = AutomaticWorker(str(tmp_path), Context(), {})
    try:
        assert worker.poll() is True
    finally:
        worker.close()


def test_success_state_survives_response_publish_failure_and_is_repaired(
        tmp_path, monkeypatch):
    import qmt_bridge.auto_worker as auto_worker
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    api_calls = []
    publish_attempts = []
    real_publish = auto_worker.publish_result

    def download(*args):
        api_calls.append(args)
        return None

    def fail_once(*args):
        publish_attempts.append(args[1])
        if len(publish_attempts) == 1:
            raise OSError('injected response write failure')
        return real_publish(*args)

    monkeypatch.setattr(auto_worker, 'publish_result', fail_once)
    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': download},
                             downloads_enabled=True)
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('download_kline', _download_args())
    try:
        assert worker.poll() is True
        state = json.loads((tmp_path / 'states' / (rid + '.json')).read_text())
        assert state['state'] == 'returned'
    finally:
        worker.close()
    worker = AutomaticWorker(str(tmp_path), object(),
                             {'download_history_data': download},
                             downloads_enabled=True)
    try:
        assert worker.poll() is True
        result = transport.lookup(rid)
        assert result['state'] == 'returned'
        assert result['data']['return_value'] is None
        assert len(api_calls) == 1
        assert len(publish_attempts) == 2
    finally:
        worker.close()


def test_queued_dispatch_does_not_enumerate_terminal_history(tmp_path, monkeypatch):
    import qmt_bridge.auto_worker as auto_worker
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport

    worker = AutomaticWorker(str(tmp_path), object(), {})
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    try:
        for number in range(4):
            rid = transport.submit('probe', {'number': number}, '%032x' % number)
            assert worker.poll() is True
            assert transport.lookup(rid)['state'] == 'returned'
        pending = transport.submit('probe', {'number': 4}, '%032x' % 4)
        real_listdir = auto_worker.os.listdir
        states = str((tmp_path / 'states').resolve())

        def bounded_listdir(path):
            if str(Path(path).resolve()) == states:
                raise AssertionError('poll must not enumerate terminal history')
            return real_listdir(path)

        monkeypatch.setattr(auto_worker.os, 'listdir', bounded_listdir)
        assert worker.poll() is True
        assert transport.lookup(pending)['state'] == 'returned'
    finally:
        worker.close()


def test_lookup_rechecks_terminal_state_after_queue_disappears(
        tmp_path, monkeypatch):
    from qmt_bridge.protocol import atomic_json, publish_result
    from bigqmt_bridge.auto_transport import AutomaticTransport

    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': 1})
    rid = transport.submit('probe', {})
    record = json.loads((tmp_path / 'records' / (rid + '.json')).read_text())
    request_path = tmp_path / 'requests' / (rid + '.json')
    running_path = tmp_path / 'running' / (rid + '.json')
    state_path = tmp_path / 'states' / (rid + '.json')
    response_path = tmp_path / 'responses' / (rid + '.json')
    envelope = {
        'auto_protocol': 'auto-kline-v1', 'request_id': rid,
        'args_hash': record['args_hash'], 'state': 'returned',
        'data': {'completed': True}, 'error': None,
    }
    real_exists = Path.exists
    transitioned = []

    def interleaved_exists(path):
        if path == state_path and not transitioned:
            return False
        if path == response_path and not transitioned:
            return False
        if path == request_path and not transitioned:
            request_path.unlink()
            atomic_json(state_path, envelope)
            publish_result(str(response_path.parent), rid, envelope, None)
            transitioned.append(True)
            return False
        if path == running_path and transitioned:
            return False
        return real_exists(path)

    monkeypatch.setattr(Path, 'exists', interleaved_exists)
    result = transport.lookup(rid)
    assert transitioned == [True]
    assert result['state'] == 'returned'
    assert result['data'] == {'completed': True}
