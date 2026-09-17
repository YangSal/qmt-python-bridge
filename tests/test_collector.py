"""Consumer tests use the real history client, worker and temporary disk journal."""
import gzip
import importlib.util
import json

import pytest

from bigqmt_bridge.auto_backend import AutomaticBackend
from qmt_bridge.auto_protocol import FileLock
from test_download_jobs import bars, Context, PumpTransport
from qmt_bridge.auto_worker import AutomaticWorker


@pytest.fixture
def rig(tmp_path):
    context = Context()
    config = {'bridge_dir': str(tmp_path / 'ipc'), 'timeout': 5,
              'poll_interval': .01, 'download_timeout': 5}
    worker = AutomaticWorker(config['bridge_dir'], context,
                             {'download_history_data': context.download}, True)
    yield context, config, PumpTransport(config, worker), worker
    worker.close()


def collector():
    assert importlib.util.find_spec('bigqmt_bridge.collector') is not None, 'collector not implemented'
    from bigqmt_bridge import collector as module
    return module


def source(rig):
    return AutomaticBackend(rig[2], rig[1])


def collect(rig, tmp_path, **kwargs):
    return collector().collect_history(source(rig), ['000001.SZ'], ['1d'], ['20260105'],
                                       tmp_path / 'output', **kwargs)


def saved_rows(tmp_path):
    path = tmp_path / 'output/history/1d/000001.SZ/20260105.jsonl.gz'
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]


def test_cold_history_download_is_archived_with_original_time(rig, tmp_path):
    report = collect(rig, tmp_path)
    assert report['state'] == 'complete'
    assert report['items'][0]['download']['items'][0]['attempted'] is True
    assert report['items'][0]['file']['rows'] == 1
    assert saved_rows(tmp_path) == [{'code': '000001.SZ', 'period': '1d',
        'date': '20260105', 'time': 1767596400000, 'open': 10.0,
        'high': 12.0, 'low': 9.0, 'close': 11.0, 'volume': 100, 'amount': 1100.0}]
    assert json.loads((tmp_path / 'output/collection.json').read_text('utf-8')) == report


def test_cache_hit_and_local_resume_are_separate_evidence(rig, tmp_path):
    rig[0].cache['000001.SZ', '1d', '20260105'] = bars()
    report = collect(rig, tmp_path)
    assert report['items'][0]['download']['items'][0]['attempted'] is False
    reads = len(rig[0].reads)
    again = collect(rig, tmp_path)
    assert again['state'] == 'complete'
    assert again['items'][0]['action'] == 'local_verified'
    assert len(rig[0].reads) == reads
    assert rig[0].downloads == []


def test_corrupt_archive_is_not_silently_overwritten(rig, tmp_path):
    collect(rig, tmp_path)
    path = tmp_path / 'output/history/1d/000001.SZ/20260105.jsonl.gz'
    path.write_bytes(b'corrupted')
    reads = len(rig[0].reads)
    report = collect(rig, tmp_path)
    assert report['state'] == 'incomplete'
    assert report['errors']
    assert path.read_bytes() == b'corrupted'
    assert len(rig[0].reads) == reads


def test_missing_archive_is_recovered_without_another_download(rig, tmp_path):
    first = collect(rig, tmp_path)
    (tmp_path / 'output/history/1d/000001.SZ/20260105.jsonl.gz').unlink()
    second = collect(rig, tmp_path)
    assert second['state'] == 'complete'
    assert first['items'][0]['download']['job_id'] == second['items'][0]['download']['job_id']
    assert len(rig[0].downloads) == 1
    assert saved_rows(tmp_path)[0]['time'] == 1767596400000


@pytest.mark.parametrize('codes,periods,dates', [
    (['../000001.SZ'], ['1d'], ['20260105']),
    (['000001.SZ'], ['tick'], ['20260105']),
    (['000001.SZ'], ['1d'], ['20260105', '20260105']),
    (['000001.SZ'], ['1d'], ['20990101']),
    (['000001.SZ'], ['1d'], []),
])
def test_invalid_scope_is_rejected_before_any_request(rig, tmp_path, codes, periods, dates):
    with pytest.raises(ValueError):
        collector().collect_history(source(rig), codes, periods, dates, tmp_path / 'output')
    assert not list((rig[2].root / 'records').glob('*.json'))
    assert not (tmp_path / 'output').exists()


def test_output_scope_cannot_change_on_resume(rig, tmp_path):
    collect(rig, tmp_path)
    before = (tmp_path / 'output/collection.json').read_bytes()
    with pytest.raises(ValueError, match='scope'):
        collector().collect_history(source(rig), ['510300.SH'], ['1d'], ['20260105'],
                                     tmp_path / 'output')
    assert (tmp_path / 'output/collection.json').read_bytes() == before


def test_archive_directory_must_be_separate_from_ipc(rig, tmp_path):
    with pytest.raises(ValueError, match='separate'):
        collector().collect_history(source(rig), ['000001.SZ'], ['1d'], ['20260105'],
                                     rig[2].root / 'archive')


def test_busy_output_lock_does_not_change_manifest_or_issue_requests(rig, tmp_path):
    output = tmp_path / 'output'
    with FileLock(output / 'collector.lock'):
        with pytest.raises(OSError):
            collect(rig, tmp_path)
    assert not (output / 'collection.json').exists()
    assert rig[0].downloads == []


def test_write_failure_is_incomplete_and_resume_keeps_job(rig, tmp_path, monkeypatch):
    module = collector()
    real_write = module.write_bars
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(module, 'write_bars', fail)
    report = collect(rig, tmp_path)
    assert report['state'] == 'incomplete'
    assert report['items'][0]['file'] is None
    job = report['items'][0]['download']['job_id']
    monkeypatch.setattr(module, 'write_bars', real_write)
    report = collect(rig, tmp_path)
    assert report['state'] == 'complete'
    assert report['items'][0]['download']['job_id'] == job
    assert len(rig[0].downloads) == 1


def test_unavailable_worker_stops_before_next_cell(rig, tmp_path, monkeypatch):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    def no_worker(request_id, timeout=None):
        raise QmtRequestTimeout(request_id)
    monkeypatch.setattr(rig[2], 'wait', no_worker)
    report = collector().collect_history(source(rig), ['000001.SZ', '510300.SH'],
                                          ['1d'], ['20260105'], tmp_path / 'output')
    assert report['state'] == 'incomplete'
    assert report['items'][0]['download']['errors']
    assert report['items'][1]['state'] == 'pending'
    assert rig[0].downloads == []


def test_nonstandard_final_read_never_archives(rig, tmp_path, monkeypatch):
    backend = source(rig)
    real_read = backend.get_market_data_ex
    def wrong_day(*args, **kwargs):
        frames = real_read(*args, **kwargs)
        frames['000001.SZ']['time'] = 1767682800000
        return frames
    monkeypatch.setattr(backend, 'get_market_data_ex', wrong_day)
    report = collector().collect_history(backend, ['000001.SZ'], ['1d'], ['20260105'],
                                          tmp_path / 'output')
    assert report['state'] == 'incomplete'
    assert not list((tmp_path / 'output').rglob('*.gz'))


def test_realtime_check_reports_missing_bridge_without_market_payload(tmp_path, capsys):
    rc = collector().main(['realtime-check', '--output-dir', str(tmp_path / 'output')])
    assert rc == 1
    report = json.loads((tmp_path / 'output/realtime-check.json').read_text('utf-8'))
    assert report['state'] == 'incomplete'
    assert report['transport_verified'] is False
    assert report['received_messages'] == 0
    assert not list((tmp_path / 'output').rglob('*.gz'))
    assert json.loads(capsys.readouterr().out)['state'] == 'incomplete'


def test_cli_uses_auto_backend_and_returns_saved_report(rig, tmp_path, monkeypatch, capsys):
    module = collector()
    monkeypatch.setattr(module, 'create_backend', lambda config: source(rig))
    cfg = tmp_path / 'bridge.json'
    cfg.write_text(json.dumps({'bridge_dir': str(rig[2].root), 'history_mode': 'auto'}))
    rc = module.main(['history', '--config', str(cfg), '--codes', '000001.SZ',
                      '--dates', '20260105', '--periods', '1d',
                      '--output-dir', str(tmp_path / 'output')])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'complete'
    assert saved_rows(tmp_path)[0]['code'] == '000001.SZ'


def test_all_existing_archives_checked_before_any_recovery(rig, tmp_path):
    module = collector()
    args = (source(rig), ['000001.SZ', '510300.SH'], ['1d'], ['20260105'], tmp_path / 'output')
    assert module.collect_history(*args)['state'] == 'complete'
    (tmp_path / 'output/history/1d/000001.SZ/20260105.jsonl.gz').unlink()
    corrupt = tmp_path / 'output/history/1d/510300.SH/20260105.jsonl.gz'
    corrupt.write_bytes(b'corrupt')
    reads = len(rig[0].reads)
    report = module.collect_history(*args)
    assert report['state'] == 'incomplete'
    assert len(rig[0].reads) == reads
    assert not (tmp_path / 'output/history/1d/000001.SZ/20260105.jsonl.gz').exists()


@pytest.mark.parametrize('period,count', [('1m', 240), ('5m', 48)])
def test_minute_archives_keep_full_grid(rig, tmp_path, period, count):
    report = collector().collect_history(source(rig), ['000001.SZ'], [period], ['20260105'],
                                          tmp_path / 'output')
    assert report['state'] == 'complete'
    with gzip.open(tmp_path / ('output/history/%s/000001.SZ/20260105.jsonl.gz' % period),
                   'rt', encoding='utf-8') as stream:
        rows = [json.loads(line) for line in stream]
    assert len(rows) == count
    assert rows[-1]['time'] == 1767596400000


def test_interruption_after_download_uses_same_durable_job(rig, tmp_path, monkeypatch):
    module = collector()
    real_save = module._save
    def interrupt(root, report):
        if report['items'][0]['download'] is not None:
            raise KeyboardInterrupt()
        real_save(root, report)
    monkeypatch.setattr(module, '_save', interrupt)
    with pytest.raises(KeyboardInterrupt):
        collect(rig, tmp_path)
    assert len(rig[0].downloads) == 1
    monkeypatch.setattr(module, '_save', real_save)
    assert collect(rig, tmp_path)['state'] == 'complete'
    assert len(rig[0].downloads) == 1


def test_unknown_native_outcome_is_not_retried_on_resume(rig, tmp_path, monkeypatch):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    transport = rig[2]
    real_wait = transport.wait
    def lose_native(request_id, timeout=None):
        if transport._load_record(request_id)['operation'] == 'download_kline':
            raise QmtRequestTimeout(request_id)
        return real_wait(request_id, timeout)
    monkeypatch.setattr(transport, 'wait', lose_native)
    first = collect(rig, tmp_path)
    assert first['state'] == 'incomplete'
    assert first['items'][0]['download']['state'] == 'unknown'
    download_ids = [path.name for path in (transport.root / 'records').glob('*.json')
                    if json.loads(path.read_text())['operation'] == 'download_kline']
    monkeypatch.setattr(transport, 'wait', real_wait)
    second = collect(rig, tmp_path)
    # The queued original request may finish during reconciliation, but no second ID is published.
    assert second['items'][0]['download']['job_id'] == first['items'][0]['download']['job_id']
    assert [path.name for path in (transport.root / 'records').glob('*.json')
            if json.loads(path.read_text())['operation'] == 'download_kline'] == download_ids
    assert len(rig[0].downloads) <= 1


def test_exhausted_budget_does_not_submit_first_cell(rig, tmp_path):
    report = collect(rig, tmp_path, max_seconds=1e-12)
    assert report['state'] == 'incomplete'
    assert report['errors']
    assert not list((rig[2].root / 'records').glob('*.json'))
