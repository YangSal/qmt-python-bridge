import ast
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def test_raw_market_reader_avoids_pandas_and_preserves_read_only_parameters():
    from qmt_bridge.worker import dispatch
    class RawContext:
        def get_market_data_ex(self, **kwargs):
            raise ModuleNotFoundError("No module named 'pandas'")
        def get_market_data_ex_ori(self, **kwargs):
            assert kwargs['subscribe'] is False and kwargs['fill_data'] is False
            assert kwargs['fields'] == ['close']
            return {'a': [['20260903', 10]]}
    result = dispatch(RawContext(), {}, 'market_data', {'fields': ['time', 'close'],
        'stock_list': ['a'], 'period': '1d', 'start_time': '20260903', 'end_time': '20260903'})
    assert result == {'__qmt_raw_market__': 1, 'fields': ['close'], 'data': {'a': [['20260903', 10]]}}


def test_strategy_restart_loads_disk_worker_instead_of_cached_class(tmp_path, monkeypatch):
    from qmt_bridge import strategy, worker
    class StaleWorker:
        def __init__(self, *args):
            raise AssertionError('cached worker must be reloaded')
    class C:
        def run_time(self, *args):
            pass
    monkeypatch.setattr(strategy, 'BRIDGE_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'Worker', StaleWorker)
    monkeypatch.setattr(strategy, 'Worker', StaleWorker, raising=False)
    try:
        strategy.init(C())
        first = strategy._worker
        strategy.init(C())
        assert first._lock is None
        assert strategy._worker.root == str(tmp_path)
    finally:
        strategy.stop(C())


class Context:
    def get_market_data_ex(self, fields, stock_code, **kwargs):
        assert kwargs['subscribe'] is False
        assert kwargs['fill_data'] is False
        return {c: [{'time': 1788418800000, 'askPrice': [1, 2, 3, 4, 5], 'close': 10}] for c in stock_code}


def wait_request(root):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if list((root / 'requests').glob('*.json')):
            return
        time.sleep(.01)
    raise AssertionError('request not published')


def test_real_file_roundtrip_and_restart_recovery(tmp_path):
    from bigqmt_bridge.transport import FileTransport
    from qmt_bridge.worker import Worker
    args = {'stock_list': ['000001.SZ'], 'fields': [], 'period': 'tick',
            'start_time': '20260903', 'end_time': '20260903', 'subscribe': False, 'fill_data': False}
    worker = Worker(str(tmp_path), Context(), {})
    client = FileTransport({'bridge_dir': str(tmp_path), 'timeout': 3, 'poll_interval': .01})
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.call, 'market_data', args)
            wait_request(tmp_path)
            req = next((tmp_path / 'requests').glob('*.json'))
            req.rename(tmp_path / 'running' / req.name)
            worker.close()
            worker = Worker(str(tmp_path), Context(), {})
            assert worker.poll()
            assert pending.result()['000001.SZ'][0]['askPrice'] == [1, 2, 3, 4, 5]
    finally:
        worker.close()


def test_second_worker_cannot_steal_requests(tmp_path):
    from qmt_bridge.worker import Worker
    worker = Worker(str(tmp_path), Context(), {})
    try:
        with pytest.raises((OSError, RuntimeError)):
            Worker(str(tmp_path), Context(), {})
    finally:
        worker.close()


def test_timeout_and_trade_rejection(tmp_path):
    from bigqmt_bridge.transport import FileTransport
    from bigqmt_bridge.backend import QmtDataError
    from qmt_bridge.worker import dispatch
    with pytest.raises(QmtDataError):
        FileTransport({'bridge_dir': str(tmp_path), 'timeout': .05, 'poll_interval': .01}).call('probe', {})
    with pytest.raises(ValueError):
        dispatch(Context(), {}, 'passorder', {})


def test_expired_request_is_not_executed(tmp_path):
    from qmt_bridge.worker import Worker
    from qmt_bridge.protocol import atomic_json
    rid = 'a' * 32
    worker = Worker(str(tmp_path), Context(), {})
    try:
        atomic_json(tmp_path / 'requests' / (rid + '.json'),
                    {'protocol': 1, 'request_id': rid, 'operation': 'probe', 'args': {}, 'deadline': time.time()-1})
        assert worker.poll()
        manifest = json.loads((tmp_path / 'responses' / (rid + '.json')).read_text())
        assert not manifest['ok']
        assert 'expired' in manifest['error']
    finally:
        worker.close()


def test_corrupt_result_rejected(tmp_path):
    from qmt_bridge.protocol import publish_result, read_result
    rid = 'b' * 32
    publish_result(str(tmp_path), rid, {'rows': [1, 2]}, None)
    (tmp_path / (rid + '.json.gz')).write_bytes(b'broken')
    with pytest.raises(ValueError):
        read_result(str(tmp_path), rid)


def test_sector_tree_and_weights_use_real_members():
    from qmt_bridge.worker import dispatch
    tree = {'': [['rootstock'], ['industry']], 'industry': [['GICS1'], []]}
    assert dispatch(Context(), {'get_sector_list': tree.__getitem__}, 'sectors', {}) == ['GICS1', 'rootstock']
    class C:
        def get_weight_in_index(self, index, code):
            return {'a': 40, 'b': 60}[code]
    assert dispatch(C(), {}, 'weights', {'index_code': 'index', 'stock_list': ['a', 'b']}) == {'a': 40, 'b': 60}


def test_qmt_files_parse_as_python36():
    for file in Path('qmt_bridge').glob('*.py'):
        ast.parse(file.read_text(encoding='utf-8'), feature_version=(3, 6))


def test_bad_filename_does_not_block_queue_and_wrong_id_is_rejected(tmp_path):
    from qmt_bridge.worker import Worker
    from qmt_bridge.protocol import atomic_json, load_json
    rid = 'b' * 32
    worker = Worker(str(tmp_path), Context(), {})
    try:
        atomic_json(tmp_path / 'requests' / '0-invalid.json', {})
        atomic_json(tmp_path / 'requests' / (rid + '.json'),
                    {'protocol': 1, 'request_id': 'c'*32, 'args': {}, 'deadline': time.time()+10})
        assert worker.poll()  # quarantine one invalid request
        assert worker.poll()  # reject mismatched UUID
        assert not load_json(tmp_path / 'responses' / (rid + '.json'))['ok']
    finally:
        worker.close()


def test_file_to_client_frame_preserves_big_int_and_book(tmp_path):
    from bigqmt_bridge.backend import InnerBackend
    from bigqmt_bridge.transport import FileTransport
    from qmt_bridge.worker import Worker
    import pandas as pd
    class C:
        def get_market_data_ex(self, **kwargs):
            assert kwargs['subscribe'] is False and kwargs['fill_data'] is False
            row = dict.fromkeys(['lastPrice', 'amount', 'open', 'high', 'low', 'lastClose',
                                 'lastSettlementPrice', 'settlementPrice', 'pe'], 1.0)
            row.update(dict.fromkeys(['pvolume', 'tickvol', 'stockStatus', 'openInt', 'transactionNum'], 1))
            row.update(time=1788418800000, volume=9007199254740993)
            row.update({k: [1, 2, 3, 4, 5] for k in ['askPrice', 'bidPrice', 'askVol', 'bidVol']})
            return {'000001.SZ': pd.DataFrame([row])}
    worker = Worker(str(tmp_path), C(), {})
    backend = InnerBackend(FileTransport({'bridge_dir': str(tmp_path), 'timeout': 3}), {'cache_prepared': True})
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(backend.get_market_data_ex, [], ['000001.SZ'], period='tick',
                                  start_time='20260903', end_time='20260903')
            wait_request(tmp_path)
            assert worker.poll()
            frame = pending.result()['000001.SZ']
            record = frame.to_dict('records')[0]
            assert record['volume'] == 9007199254740993
            assert record['askPrice'][4] == 5 and record['bidVol'][4] == 5
            assert pd.to_datetime(record['time'], unit='ms', utc=True).tz_convert('Asia/Shanghai').isoformat() == '2026-09-03T15:00:00+08:00'
    finally:
        worker.close()
