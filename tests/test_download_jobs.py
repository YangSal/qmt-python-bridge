"""Automatic jobs exercise the actual disk journal and worker in temporary roots."""
import json

import pandas as pd
import pytest

from bigqmt_bridge import QmtDataError
from bigqmt_bridge.auto_transport import AutomaticTransport, QmtRequestTimeout
from qmt_bridge.auto_worker import AutomaticWorker


def bars(date='20260105', period='1d'):
    if period == '1d':
        times = pd.DatetimeIndex([pd.Timestamp(date + ' 15:00', tz='Asia/Shanghai')])
    else:
        step = 1 if period == '1m' else 5
        times = pd.date_range(date + ' 09:30', date + ' 11:30', freq='%smin' % step,
                              tz='Asia/Shanghai')[1:].append(
            pd.date_range(date + ' 13:00', date + ' 15:00', freq='%smin' % step,
                          tz='Asia/Shanghai')[1:])
    return [{'time': int(t.value // 1000000), 'stime': 'wrong-local-time',
             'open': 10., 'high': 12., 'low': 9., 'close': 11.,
             'volume': 100, 'amount': 1100.} for t in times]


class Context:
    def __init__(self):
        self.cache = {}
        self.fail_codes = set()
        self.incomplete_codes = set()
        self.downloads = []
        self.reads = []

    def download(self, code, period, start, end):
        assert start == end
        self.downloads.append((code, period, start))
        if code in self.fail_codes:
            return False
        self.cache[code, period, start] = ([] if code in self.incomplete_codes
                                         else bars(start, period))

    def get_market_data_ex_ori(self, **kwargs):
        assert kwargs['subscribe'] is False and kwargs['fill_data'] is False
        assert kwargs['dividend_type'] == 'none'
        self.reads.append(kwargs)
        return {code: self.cache.get((code, kwargs['period'], kwargs['start_time']), [])
                for code in kwargs['stock_code']}


class PumpTransport(AutomaticTransport):
    """Only the wait boundary drives a real worker; all file I/O is real."""
    def __init__(self, config, worker):
        super().__init__(config)
        self.worker = worker

    def wait(self, request_id, timeout=None):
        self.worker.poll()
        return super().wait(request_id, timeout)


@pytest.fixture
def rig(tmp_path):
    context = Context()
    config = {'bridge_dir': str(tmp_path), 'timeout': 2, 'poll_interval': .01,
              'download_timeout': 2}
    worker = AutomaticWorker(str(tmp_path), context,
                             {'download_history_data': context.download}, True)
    transport = PumpTransport(config, worker)
    yield context, config, transport, worker
    worker.close()


def manager(rig):
    from bigqmt_bridge.downloads import DownloadManager
    return DownloadManager(rig[2], rig[1])


def test_cold_cache_cross_product_is_verified_and_durable(rig):
    report = manager(rig).download(['510300.SH', '000001.SZ', '000001.SZ'], '1d',
                                   '20260105', '20260106',
                                   expected_dates=['20260105', '20260106'])
    assert report['state'] == 'verified'
    assert report['totals']['verified'] == 4 and report['totals']['total'] == 4
    assert len(rig[0].downloads) == 4
    assert len({item['request_id'] for item in report['items']}) == 4
    assert all(item['validation']['rows'] == 1 for item in report['items'])
    assert manager(rig).status(report['job_id']) == report
    again = manager(rig).download(['000001.SZ', '510300.SH'], '1d', '20260105',
                                  '20260106', ['20260105', '20260106'])
    assert again['job_id'] == report['job_id']
    assert len(rig[0].downloads) == 4
    assert 'open' not in json.dumps(report)


def test_warm_cache_has_no_native_download_and_is_revalidated(rig):
    rig[0].cache['000001.SZ', '1d', '20260105'] = bars()
    report = manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert not rig[0].downloads
    rig[0].cache.clear()
    report2 = manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert report2['job_id'] == report['job_id'] and len(rig[0].downloads) == 1


def test_explicit_id_binds_scope_before_any_new_publish(rig):
    task = manager(rig)
    task.download(['000001.SZ'], '1d', '20260105', '20260105', job_id='a' * 32)
    count = len(list(rig[2].root.joinpath('records').glob('*.json')))
    with pytest.raises(QmtDataError, match='bound|scope|parameters'):
        task.download(['510300.SH'], '1d', '20260105', '20260105', job_id='a' * 32)
    assert len(list(rig[2].root.joinpath('records').glob('*.json'))) == count


@pytest.mark.parametrize('changes', [
    {'stock_list': []}, {'stock_list': ['000001']}, {'period': 'tick'},
    {'start_time': '2026-01-05'}, {'end_time': '20990101'},
    {'end_time': '20260106'},
    {'end_time': '20260106', 'expected_dates': ['20260106', '20260105']},
    {'end_time': '20260106', 'expected_dates': ['20260105']},
    {'expected_dates': ['20260105', '20260105']},
    {'start_time': '20240101', 'end_time': '20260105',
     'expected_dates': ['20240101', '20260105']},
    {'stock_list': ['%06d.SZ' % i for i in range(10001)]},
    {'stock_list': ['%06d.SZ' % i for i in range(7000)], 'end_time': '20260107',
     'expected_dates': ['20260105', '20260106', '20260107']},
    {'job_id': '../unsafe'}, {'job_id': 'a' * 32 + '\n'},
])
def test_scope_rejected_before_any_publish(rig, changes):
    args = dict(stock_list=['000001.SZ'], period='1d', start_time='20260105', end_time='20260105')
    args.update(changes)
    with pytest.raises(QmtDataError):
        manager(rig).download(**args)
    assert not list((rig[2].root / 'records').glob('*.json'))


def test_old_worker_is_rejected_before_download(tmp_path):
    from bigqmt_bridge.downloads import DownloadManager
    class Old:
        def call(self, op, args):
            assert op == 'probe'
            return {'worker_version': 2}
    with pytest.raises(QmtDataError):
        DownloadManager(Old(), {'bridge_dir': str(tmp_path)}).download(
            ['000001.SZ'], '1d', '20260105', '20260105')


@pytest.mark.parametrize('probe', [None, {}, {'worker_version': '3',
    'automatic_kline': 'auto-kline-v1', 'downloads_enabled': True},
    {'worker_version': 3, 'automatic_kline': 'auto-kline-v1', 'downloads_enabled': False}])
def test_bad_or_disabled_probe_is_explicit_error_before_native(tmp_path, probe):
    from bigqmt_bridge.downloads import DownloadManager
    class BadWorker:
        def call(self, op, args):
            assert op == 'probe'
            return probe
    with pytest.raises(QmtDataError):
        DownloadManager(BadWorker(), {'bridge_dir': str(tmp_path)}).download(
            ['000001.SZ'], '1d', '20260105', '20260105')


def test_failed_and_incomplete_cells_produce_partial_counts(rig):
    from bigqmt_bridge.downloads import QmtDownloadError
    rig[0].fail_codes.add('000002.SZ')
    rig[0].incomplete_codes.add('000003.SZ')
    with pytest.raises(QmtDownloadError) as error:
        manager(rig).download(['000001.SZ', '000002.SZ', '000003.SZ'],
                              '1d', '20260105', '20260105')
    report = error.value.report
    assert report['state'] == 'partial'
    assert [i['state'] for i in report['items']] == ['verified', 'failed', 'incomplete']
    assert report['totals']['verified'] == 1 and report['totals']['failed'] == 1
    assert report['totals']['incomplete'] == 1
    assert len(rig[0].downloads) == 3
    with pytest.raises(QmtDownloadError):
        manager(rig).download(['000001.SZ', '000002.SZ', '000003.SZ'],
                              '1d', '20260105', '20260105')
    assert len(rig[0].downloads) == 3


def test_unknown_stops_new_downloads_and_resume_only_reconciles(rig):
    from bigqmt_bridge.downloads import QmtDownloadError
    transport = rig[2]
    original = transport.submit
    def lose_publication(op, args, request_id=None):
        request_id = original(op, args, request_id)
        if op == 'download_kline':
            (transport.root / 'requests' / (request_id + '.json')).unlink()
            raise QmtRequestTimeout(request_id)
        return request_id
    transport.submit = lose_publication
    with pytest.raises(QmtDownloadError) as error:
        manager(rig).download(['000001.SZ', '000002.SZ'], '1d', '20260105', '20260105')
    report = error.value.report
    assert report['state'] == 'unknown'
    assert [i['state'] for i in report['items']] == ['unknown', 'pending']
    request_id = report['items'][0]['request_id']
    transport.submit = original
    with pytest.raises(QmtDownloadError) as resumed:
        manager(rig).download(['000001.SZ', '000002.SZ'], '1d', '20260105', '20260105')
    assert resumed.value.report['items'][0]['request_id'] == request_id
    assert not rig[0].downloads
    rig[0].cache['000001.SZ', '1d', '20260105'] = bars()
    done = manager(rig).download(['000001.SZ', '000002.SZ'], '1d', '20260105', '20260105')
    assert done['state'] == 'verified'
    assert rig[0].downloads == [('000002.SZ', '1d', '20260105')]


def test_downloaded_verified_cache_removed_is_never_downloaded_again(rig):
    from bigqmt_bridge.downloads import QmtDownloadError
    manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    rig[0].cache.clear()
    with pytest.raises(QmtDownloadError) as error:
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert error.value.report['items'][0]['state'] == 'incomplete'
    assert len(rig[0].downloads) == 1


@pytest.mark.parametrize('period,expected', [('1d', 1), ('1m', 240), ('5m', 48)])
def test_exact_grid_and_original_utc_are_verified(period, expected):
    from bigqmt_bridge.kline import validate_kline
    result = validate_kline({'000001.SZ': bars(period=period)}, '000001.SZ', period, '20260105')
    assert result['rows'] == expected
    assert result['last_beijing'] == '2026-01-05T15:00:00+08:00'


@pytest.mark.parametrize('period', ['1m', '5m'])
def test_optional_0930_bar_allowed(period):
    from bigqmt_bridge.kline import validate_kline
    data = bars(period=period)
    extra = dict(data[0], time=int(pd.Timestamp('20260105 09:30', tz='Asia/Shanghai').value // 1000000))
    assert validate_kline({'000001.SZ': [extra] + data}, '000001.SZ', period,
                          '20260105')['rows'] == len(data) + 1


@pytest.mark.parametrize('fault', ['missing_time', 'string_time', 'fractional_time', 'boolean_time',
                                  'duplicate', 'wrong_day', 'missing_field', 'nan', 'negative',
                                  'bad_ohlc', 'negative_prices', 'missing_minute', 'off_grid'])
def test_invalid_kline_is_not_repaired(fault):
    from bigqmt_bridge.kline import validate_kline
    data = bars(period='1m')
    if fault == 'missing_time':
        for row in data:
            row['stime'] = '20260105093100'
            del row['time']
    elif fault == 'string_time': data[0]['time'] = str(data[0]['time'])
    elif fault == 'fractional_time': data[0]['time'] += .5
    elif fault == 'boolean_time': data[0]['time'] = True
    elif fault == 'duplicate': data[1]['time'] = data[0]['time']
    elif fault == 'wrong_day': data[0]['time'] -= 86400000
    elif fault == 'missing_field': del data[0]['open']
    elif fault == 'nan': data[0]['amount'] = float('nan')
    elif fault == 'negative': data[0]['volume'] = -1
    elif fault == 'bad_ohlc': data[0]['close'] = 20
    elif fault == 'negative_prices': data[0].update(open=-10, high=-9, low=-12, close=-11)
    elif fault == 'missing_minute': data.pop()
    elif fault == 'off_grid': data[0]['time'] += 1000
    with pytest.raises(QmtDataError):
        validate_kline({'000001.SZ': data}, '000001.SZ', '1m', '20260105')


def test_corrupt_native_journal_is_not_hidden_by_valid_cache(rig):
    from bigqmt_bridge.downloads import QmtDownloadError
    report = manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    request_id = report['items'][0]['request_id']
    (rig[2].root / 'states' / (request_id + '.json')).write_text('{}')
    with pytest.raises(QmtDownloadError) as error:
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert error.value.report['state'] == 'unknown'
    assert len(rig[0].downloads) == 1


def test_status_is_local_and_damaged_report_is_rejected(rig):
    report = manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    count = len(list((rig[2].root / 'records').glob('*.json')))
    assert manager(rig).status(report['job_id']) == report
    assert len(list((rig[2].root / 'records').glob('*.json'))) == count
    path = rig[2].root / 'client_jobs' / (report['job_id'] + '.json')
    report['items'][0]['request_id'] = 'b' * 32
    path.write_text(json.dumps(report))
    with pytest.raises(QmtDataError):
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert len(list((rig[2].root / 'records').glob('*.json'))) == count


def test_job_os_lock_prevents_concurrent_advancement(rig):
    from qmt_bridge.auto_protocol import FileLock
    task = manager(rig)
    report = task.download(['000001.SZ'], '1d', '20260105', '20260105')
    count = len(list((rig[2].root / 'records').glob('*.json')))
    with FileLock(rig[2].root / 'client_jobs' / (report['job_id'] + '.lock')):
        with pytest.raises(QmtDataError, match='locked'):
            task.download(['000001.SZ'], '1d', '20260105', '20260105')
    assert len(list((rig[2].root / 'records').glob('*.json'))) == count


def test_expired_cell_budget_never_starts_native_download(rig):
    from bigqmt_bridge.downloads import QmtDownloadError
    rig[1]['download_timeout'] = .000001
    with pytest.raises(QmtDownloadError):
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert not rig[0].downloads


def test_crash_after_attempt_saved_before_submit_does_not_replay(rig, monkeypatch):
    from bigqmt_bridge import downloads
    class Crash(BaseException):
        pass
    original = downloads.atomic_json
    def crash(path, report):
        original(path, report)
        if report['items'][0]['state'] == 'running':
            raise Crash()
    monkeypatch.setattr(downloads, 'atomic_json', crash)
    with pytest.raises(Crash):
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    monkeypatch.setattr(downloads, 'atomic_json', original)
    with pytest.raises(downloads.QmtDownloadError) as resumed:
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert resumed.value.report['state'] == 'unknown'
    assert not rig[0].downloads


def test_timeout_ack_loss_reconciles_original_request_without_resubmit(rig):
    from bigqmt_bridge.downloads import QmtDownloadError
    original = rig[2].wait
    def lose_ack(request_id, timeout=None):
        response = original(request_id, timeout)
        record = json.loads((rig[2].root / 'records' / (request_id + '.json')).read_text())
        if record['operation'] == 'download_kline':
            raise QmtRequestTimeout(request_id)
        return response
    rig[2].wait = lose_ack
    with pytest.raises(QmtDownloadError) as error:
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert error.value.report['state'] == 'unknown'
    request_id = error.value.report['items'][0]['request_id']
    rig[2].wait = original
    report = manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert report['state'] == 'verified' and report['items'][0]['request_id'] == request_id
    assert len(rig[0].downloads) == 1


def test_callback_mutation_cannot_rebind_remaining_downloads(rig):
    observed = []
    def callback(report):
        observed.append(report['totals']['verified'])
        report['items'][1]['code'] = '999999.SZ'
        report['request']['period'] = 'tick'
    report = manager(rig).download(['000001.SZ', '510300.SH'], '1d',
                                   '20260105', '20260105', callback=callback)
    assert report['state'] == 'verified' and observed == [1, 2]
    assert rig[0].downloads == [('000001.SZ', '1d', '20260105'), ('510300.SH', '1d', '20260105')]


@pytest.mark.parametrize('period,code', [('1m', '000300.SH'), ('5m', '510300.SH')])
def test_minute_and_index_cold_cache_uses_strict_grid(rig, period, code):
    report = manager(rig).download([code], period, '20260105', '20260105')
    assert report['items'][0]['validation']['rows'] == (240 if period == '1m' else 48)


@pytest.mark.parametrize('shape', ['columns', 'positional', 'frame'])
def test_complete_original_time_shapes_are_accepted(shape):
    from bigqmt_bridge.kline import validate_kline
    row = bars()[0]
    if shape == 'columns':
        value = {key: [item] for key, item in row.items()}
    elif shape == 'positional':
        row = {'stime': row.pop('stime'), **row}
        value = [list(row.values())]
    else:
        value = {'__frame__': True, 'columns': list(row), 'index': ['local-wrong'], 'data': [row]}
    raw = {'__qmt_raw_market__': 1, 'fields': [key for key in row if key != 'stime'],
           'data': {'000001.SZ': value}}
    assert validate_kline(raw, '000001.SZ', '1d', '20260105')['rows'] == 1


@pytest.mark.parametrize('fault', ['aggregate', 'totals', 'boolean_total', 'validation_missing',
    'rows', 'date', 'naive_time', 'different_daily_end', 'verified_error', 'pending_evidence',
    'failed_unattempted', 'failure_without_error', 'missing_timestamp'])
def test_status_rejects_inconsistent_state_and_evidence(rig, fault):
    rig[0].cache['000001.SZ', '1d', '20260105'] = bars()
    report = manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    item = report['items'][0]
    if fault == 'aggregate':
        item.update(state='pending', validation=None)
    elif fault == 'totals': report['totals']['verified'] = 0
    elif fault == 'boolean_total': report['totals']['total'] = True
    elif fault == 'validation_missing': item['validation'] = None
    elif fault == 'rows': item['validation']['rows'] = 0
    elif fault == 'date': item['validation']['first_beijing'] = '2026-01-06T15:00:00+08:00'
    elif fault == 'naive_time': item['validation']['first_beijing'] = '2026-01-05T15:00:00'
    elif fault == 'different_daily_end': item['validation']['last_beijing'] = '2026-01-05T15:01:00+08:00'
    elif fault == 'verified_error': item['error'] = 'previous failure'
    elif fault == 'pending_evidence':
        item['state'] = report['state'] = 'pending'
        report['totals'].update(verified=0, pending=1)
    elif fault == 'failed_unattempted':
        item.update(state='failed', validation=None, error='native failed')
        report['state'] = 'failed'
        report['totals'].update(verified=0, failed=1)
    elif fault == 'failure_without_error':
        item.update(state='incomplete', validation=None)
        report['state'] = 'incomplete'
        report['totals'].update(verified=0, incomplete=1)
    elif fault == 'missing_timestamp': del item['updated_at']
    path = rig[2].root / 'client_jobs' / (report['job_id'] + '.json')
    path.write_text(json.dumps(report))
    count = len(list((rig[2].root / 'records').glob('*.json')))
    with pytest.raises(QmtDataError):
        manager(rig).status(report['job_id'])
    with pytest.raises(QmtDataError):
        manager(rig).download(['000001.SZ'], '1d', '20260105', '20260105')
    assert len(list((rig[2].root / 'records').glob('*.json'))) == count


@pytest.mark.parametrize('native_state,expected', [('returned', 'incomplete'),
                                                 ('failed', 'failed'), ('expired', 'failed')])
def test_resume_read_timeout_preserves_known_native_outcome(rig, native_state, expected):
    from bigqmt_bridge.downloads import QmtDownloadError
    from qmt_bridge.protocol import atomic_json
    class StopAfterFirst(Exception):
        pass
    if native_state == 'failed':
        rig[0].fail_codes.add('000001.SZ')
    if native_state == 'expired':
        submit = rig[2].submit
        def expire(op, args, request_id=None):
            request_id = submit(op, args, request_id)
            if op == 'download_kline' and args['stock_code'] == '000001.SZ':
                path = rig[2].root / 'requests' / (request_id + '.json')
                request = json.loads(path.read_text())
                request['deadline'] = 1
                atomic_json(path, request)
            return request_id
        rig[2].submit = expire
    def stop(report):
        raise StopAfterFirst()
    with pytest.raises(StopAfterFirst):
        manager(rig).download(['000001.SZ', '000002.SZ'], '1d', '20260105', '20260105', callback=stop)
    original_wait = rig[2].wait
    def read_timeout(request_id, timeout=None):
        result = original_wait(request_id, timeout)
        record = json.loads((rig[2].root / 'records' / (request_id + '.json')).read_text())
        if record['operation'] == 'market_data' and record['args']['stock_list'] == ['000001.SZ']:
            raise QmtRequestTimeout(request_id)
        return result
    rig[2].wait = read_timeout
    with pytest.raises(QmtDownloadError) as resumed:
        manager(rig).download(['000001.SZ', '000002.SZ'], '1d', '20260105', '20260105')
    report = resumed.value.report
    assert report['state'] == 'partial'
    assert [item['state'] for item in report['items']] == [expected, 'verified']
    assert rig[0].downloads.count(('000001.SZ', '1d', '20260105')) == (0 if native_state == 'expired' else 1)
    if native_state in ('failed', 'expired'):
        assert ('failure' if native_state == 'failed' else 'expired') in report['items'][0]['error']
