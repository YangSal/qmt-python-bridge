"""Offline safety checks for the throwaway P0/P1 qualification harness."""
import importlib.util
import time
from pathlib import Path

import pytest


def harness():
    path = Path(__file__).with_name('worker.py')
    assert path.is_file(), 'qualification worker not implemented'
    spec = importlib.util.spec_from_file_location('qualification_test_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(operation, **args):
    return {'protocol': 'qualification-v1', 'request_id': 'a' * 32,
            'deadline': time.time() + 30, 'operation': operation, 'args': args}


def history_args():
    return dict(stock_code='000001.SZ', period='1d', date='20260908')


def test_none_download_return_is_not_data_ready():
    calls = []
    api = {'download_history_data': lambda *a: calls.append(a)}
    result = harness().dispatch(object(), api, request('download_history', **history_args()))
    assert calls == [('000001.SZ', '1d', '20260908', '20260908')]
    assert result['state'] == 'request_returned'
    assert result['data_ready'] is False


def test_absent_downloader_is_an_explicit_failure():
    with pytest.raises(NotImplementedError):
        harness().dispatch(object(), {}, request('download_history', **history_args()))


@pytest.mark.parametrize('operation', ['passorder', 'cancel', 'eval', 'execute'])
def test_unknown_and_trade_operations_are_rejected(operation):
    with pytest.raises(ValueError, match='operation'):
        harness().dispatch(object(), {}, request(operation))


@pytest.mark.parametrize('bad', [dict(stock_code='600519.SH'), dict(period='1mon'),
                               dict(date='20260909'), dict(date='20260230')])
def test_download_is_restricted_to_the_reviewed_sample(bad):
    args = dict(history_args(), **bad)
    with pytest.raises(ValueError):
        harness().dispatch(object(), {}, request('download_history', **args))


def test_expired_request_never_calls_native_downloader():
    def forbidden(*args):
        pytest.fail('expired request reached native API')
    req = request('download_history', **history_args())
    req['deadline'] = time.time() - 1
    with pytest.raises(ValueError, match='expired'):
        harness().dispatch(object(), {'download_history_data': forbidden}, req)


def test_history_read_preserves_strict_parameters_and_raw_shape():
    class Context:
        def get_market_data_ex_ori(self, **kwargs):
            assert kwargs == dict(fields=['open', 'high', 'low', 'close', 'volume', 'amount'],
                                  stock_code=['000001.SZ'], period='1d',
                                  start_time='20260908', end_time='20260908', count=-1,
                                  dividend_type='none', fill_data=False, subscribe=False)
            return {'000001.SZ': [['20260908', 10, 11, 9, 10, 100, 100000]]}
    result = harness().dispatch(Context(), {}, request('read_history', **history_args()))
    assert result['__qmt_raw_market__'] == 1
    assert result['data']['000001.SZ'][0][0] == '20260908'


def test_negative_download_result_is_not_success():
    with pytest.raises(RuntimeError, match='rejected'):
        harness().dispatch(object(), {'download_history_data': lambda *a: -1},
                           request('download_history', **history_args()))


def test_file_worker_does_not_replay_an_interrupted_download(tmp_path):
    from qmt_bridge.protocol import atomic_json, read_result
    module = harness()
    assert hasattr(module, 'QualificationWorker'), 'file harness not implemented'
    calls = []
    api = {'download_history_data': lambda *a: calls.append(a)}
    worker = module.QualificationWorker(str(tmp_path), object(), api)
    interrupted = tmp_path / 'running' / ('b' * 32 + '.json')
    old = request('download_history', **history_args())
    old['request_id'] = 'b' * 32
    atomic_json(interrupted, old)
    worker.close()
    worker = module.QualificationWorker(str(tmp_path), object(), api)
    try:
        assert not worker.poll()
        assert interrupted.exists()
        assert calls == []
        atomic_json(tmp_path / 'requests' / ('a' * 32 + '.json'),
                    request('download_history', **history_args()))
        assert worker.poll()
        result = read_result(str(tmp_path / 'responses'), 'a' * 32)
        assert result['experiment'] == 'qualification-v1'
        assert result['data']['data_ready'] is False
        assert len(calls) == 1
    finally:
        worker.close()


def test_capability_probe_only_checks_existence():
    def forbidden(*args):
        pytest.fail('probe invoked a downloader or trader')
    api = {'download_history_data': forbidden, 'passorder': forbidden}
    result = harness().dispatch(object(), api, request('capabilities'))
    assert result['globals']['download_history_data'] is True
    assert result['globals']['passorder'] is True
    assert result['trading_enabled'] is False


def client_harness():
    path = Path(__file__).with_name('client.py')
    assert path.is_file(), 'qualification client not implemented'
    spec = importlib.util.spec_from_file_location('qualification_test_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_download_result_cannot_be_used_as_readiness_evidence():
    with pytest.raises(ValueError):
        client_harness().validate_sample({'state': 'request_returned'}, '000001.SZ', '1d')


def test_daily_readiness_checks_real_date_ohlc_and_count():
    raw = {'__qmt_raw_market__': 1,
           'fields': ['open', 'high', 'low', 'close', 'volume', 'amount'],
           'data': {'000001.SZ': [['20260908', 10, 11, 9, 10, 100, 100000]]}}
    assert client_harness().validate_sample(raw, '000001.SZ', '1d')['rows'] == 1
    raw['data']['000001.SZ'][0][0] = '20260907'
    with pytest.raises(ValueError, match='date'):
        client_harness().validate_sample(raw, '000001.SZ', '1d')


@pytest.mark.parametrize('shape', ['positional', 'columns'])
def test_real_file_download_to_readable_sample(tmp_path, shape):
    from concurrent.futures import ThreadPoolExecutor
    cache, downloads = [], []
    class Context:
        def get_market_data_ex_ori(self, **kwargs):
            if shape == 'columns':
                columns = ['stime', 'open', 'high', 'low', 'close', 'volume', 'amount']
                return {'000001.SZ': {k: [row[i] for row in cache] for i, k in enumerate(columns)}}
            return {'000001.SZ': list(cache)}
    def downloader(*args):
        downloads.append(args)
        cache.append(['20260908', 10, 11, 9, 10, 100, 100000])
    worker = harness().QualificationWorker(str(tmp_path), Context(), {'download_history_data': downloader})
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client_harness().sample, str(tmp_path), '000001.SZ', '1d', True)
            deadline = time.monotonic() + 5
            while not pending.done() and time.monotonic() < deadline:
                worker.poll()
                time.sleep(.01)
            result = pending.result(timeout=1)
        assert result['ok'] is True
        assert result['initial_cache_empty'] is True
        assert result['cold_cache_to_ready'] is True
        assert len(downloads) == 1
        assert result['download']['data']['data_ready'] is False
    finally:
        worker.close()


def test_same_count_with_missing_minute_does_not_pass():
    import pandas as pd
    times = list(pd.date_range('2026-09-08 09:31', '2026-09-08 11:30', freq='min'))
    times += list(pd.date_range('2026-09-08 13:01', '2026-09-08 15:00', freq='min'))
    times[20] = pd.Timestamp('2026-09-08 12:01')
    raw = {'__qmt_raw_market__': 1, 'fields': ['open','high','low','close','volume','amount'],
           'data': {'000001.SZ': [[t.strftime('%Y%m%d%H%M%S'),10,11,9,10,100,100000] for t in times]}}
    with pytest.raises(ValueError, match='minute'):
        client_harness().validate_sample(raw, '000001.SZ', '1m')
