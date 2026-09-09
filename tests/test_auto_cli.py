import json

import pytest


def _report(state='verified', job_id='a' * 32):
    return {
        'job_id': job_id,
        'state': state,
        'request': {
            'stock_list': ['000001.SZ'],
            'period': '1d',
            'start_time': '20260908',
            'end_time': '20260908',
            'expected_dates': ['20260908'],
        },
        'totals': {'verified': 1 if state == 'verified' else 0, 'total': 1},
        'items': [{
            'code': '000001.SZ',
            'date': '20260908',
            'request_id': 'b' * 32,
            'state': state,
            'validation': ({'rows': 1,
                            'first_beijing': '2026-09-08T15:00:00+08:00',
                            'last_beijing': '2026-09-08T15:00:00+08:00'}
                           if state == 'verified' else None),
        }],
    }


def test_download_writes_verified_report_and_forces_auto_scope(tmp_path, monkeypatch):
    """A wrong handler/config branch would lose the exact scope or use cache-only mode."""
    from bigqmt_bridge import cli

    observed = {}
    report = _report()

    class Manager:
        def download(self, *args):
            observed['args'] = args
            return report

    class Backend:
        downloads = Manager()

    def create(config):
        observed['config'] = config
        return Backend()

    monkeypatch.setattr(cli, 'create_backend', create)
    config = tmp_path / 'auto.json'
    config.write_text(json.dumps({
        'backend': 'file_bridge',
        'bridge_dir': 'unused-runtime',
        'history_mode': 'cache_only',
    }), encoding='utf-8')
    output = tmp_path / 'verified.json'

    code = cli.main([
        'download', '--config', str(config), '--bridge-dir', str(tmp_path / 'runtime'),
        '--codes', '510300.SH,000001.SZ', '--period', '1d',
        '--start', '20260908', '--end', '20260909',
        '--expected-dates', '20260908,20260909', '--job-id', 'a' * 32,
        '--output', str(output),
    ])

    assert code == 0
    assert json.loads(output.read_text(encoding='utf-8')) == report
    assert observed['config']['history_mode'] == 'auto'
    assert observed['config']['bridge_dir'] == str(tmp_path / 'runtime')
    assert observed['args'] == (
        ['510300.SH', '000001.SZ'], '1d', '20260908', '20260909',
        ['20260908', '20260909'], 'a' * 32,
    )


def test_download_preserves_qmt_download_error_report_and_ids(tmp_path, monkeypatch):
    """A generic exception wrapper would discard durable job and request identities."""
    from bigqmt_bridge import cli
    from bigqmt_bridge.downloads import QmtDownloadError

    report = _report('incomplete')

    class Manager:
        def download(self, *args):
            raise QmtDownloadError(report)

    class Backend:
        downloads = Manager()

    monkeypatch.setattr(cli, 'create_backend', lambda config: Backend())
    output = tmp_path / 'incomplete.json'

    code = cli.main([
        'download', '--bridge-dir', str(tmp_path / 'runtime'),
        '--codes', '000001.SZ', '--period', '1d', '--start', '20260908',
        '--end', '20260908', '--output', str(output),
    ])

    saved = json.loads(output.read_text(encoding='utf-8'))
    assert code == 1
    assert saved == report
    assert saved['job_id'] == 'a' * 32
    assert saved['items'][0]['request_id'] == 'b' * 32


@pytest.mark.parametrize('state,expected_code', [('verified', 0), ('unknown', 1)])
def test_download_status_is_local_only(tmp_path, monkeypatch, state, expected_code):
    """Status must not create a transport or send a QMT probe/request."""
    from bigqmt_bridge import cli

    report = _report(state)
    observed = {}

    class LocalManager:
        def __init__(self, transport, config):
            observed['transport'] = transport
            observed['config'] = config

        def status(self, job_id):
            observed['job_id'] = job_id
            return report

    monkeypatch.setattr(cli, 'DownloadManager', LocalManager)
    monkeypatch.setattr(cli, 'create_backend', lambda config: pytest.fail('status created a backend'))
    output = tmp_path / ('status-' + state + '.json')

    code = cli.main([
        'download-status', '--bridge-dir', str(tmp_path / 'runtime'),
        '--job-id', 'a' * 32, '--output', str(output),
    ])

    assert code == expected_code
    assert json.loads(output.read_text(encoding='utf-8')) == report
    assert observed['transport'] is None
    assert observed['config']['history_mode'] == 'auto'
    assert observed['job_id'] == 'a' * 32


def test_download_refuses_native_and_persists_requested_job_id(tmp_path, monkeypatch):
    """Native baseline mode must never become an automatic-download transport."""
    from bigqmt_bridge import cli

    config = tmp_path / 'native.json'
    config.write_text(json.dumps({'backend': 'native'}), encoding='utf-8')
    monkeypatch.setattr(cli, 'create_backend', lambda value: pytest.fail('native backend created'))
    output = tmp_path / 'native-error.json'

    code = cli.main([
        'download', '--config', str(config), '--codes', '000001.SZ',
        '--period', '1d', '--start', '20260908', '--end', '20260908',
        '--job-id', 'c' * 32, '--output', str(output),
    ])

    saved = json.loads(output.read_text(encoding='utf-8'))
    assert code == 1
    assert saved['ok'] is False
    assert saved['job_id'] == 'c' * 32
    assert 'native' in saved['errors'][0]


def test_legacy_compare_behavior_is_unchanged(tmp_path):
    """Adding command dispatch must not change the existing compare result contract."""
    from bigqmt_bridge.cli import main

    sample = {
        'version': 1,
        'date': '20260908',
        'codes': ['000001.SZ'],
        'periods': ['1d'],
        'families': ['market'],
        'errors': [],
        'samples': {'market': {'1d': {'000001.SZ': {
            'columns': ['time', 'open', 'high', 'low', 'close', 'volume', 'amount'],
            'rows': [[1788850800000, 10, 11, 9, 10, 100, 1000]],
        }}}},
    }
    left, right, output = tmp_path / 'left.json', tmp_path / 'right.json', tmp_path / 'diff.json'
    left.write_text(json.dumps(sample), encoding='utf-8')
    right.write_text(json.dumps(sample), encoding='utf-8')

    assert main(['compare', str(left), str(right), '--output', str(output)]) == 0
    assert json.loads(output.read_text(encoding='utf-8')) == {'ok': True, 'errors': []}
