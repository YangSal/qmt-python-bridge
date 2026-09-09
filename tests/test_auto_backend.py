import json

import pytest

from bigqmt_bridge import QmtDataError
from bigqmt_bridge.backend import InnerBackend, create_backend
from bigqmt_bridge.config import load_config, validate_config
from test_download_jobs import bars, rig


def test_auto_factory_and_default_cache_gate(tmp_path):
    from bigqmt_bridge.auto_backend import AutomaticBackend
    assert isinstance(create_backend({'bridge_dir': str(tmp_path), 'history_mode': 'auto'}), AutomaticBackend)
    legacy = create_backend({'bridge_dir': str(tmp_path)})
    assert type(legacy) is InnerBackend
    with pytest.raises(QmtDataError, match='cache_prepared'):
        legacy.get_market_data_ex(stock_list=['000001.SZ'])


def test_auto_cold_download_and_read_without_manual_gate(rig):
    from bigqmt_bridge.auto_backend import AutomaticBackend
    backend = AutomaticBackend(rig[2], dict(rig[1], cache_prepared=False))
    report = backend.download_history_data2(['000001.SZ'], '1d', '20260105', '20260105')
    assert backend.download_status(report['job_id']) == report
    data = backend.get_market_data_ex(['close'], ['000001.SZ'], '1d', '20260105', '20260105')
    assert data['000001.SZ'].iloc[0]['close'] == 11


def test_auto_reads_reject_stime_only_before_legacy_normalization(rig):
    from bigqmt_bridge.auto_backend import AutomaticBackend
    rows = bars()
    del rows[0]['time']
    rows[0]['stime'] = '20260105150000'
    rig[0].cache['000001.SZ', '1d', '20260105'] = rows
    with pytest.raises(QmtDataError, match='time|UTC'):
        AutomaticBackend(rig[2], rig[1]).get_market_data_ex(
            ['close'], ['000001.SZ'], '1d', '20260105', '20260105')


def test_auto_explicitly_rejects_unsupported_operations_and_kwargs(rig):
    from bigqmt_bridge.auto_backend import AutomaticBackend
    backend = AutomaticBackend(rig[2], rig[1])
    calls = [lambda: backend.download_financial_data2(['000001.SZ']),
             lambda: backend.download_index_weight(),
             lambda: backend.get_market_data_ex(stock_list=['000001.SZ'], period='tick'),
             lambda: backend.get_market_data_ex(stock_list=['000001.SZ'], dividend_type='front'),
             lambda: backend.download_history_data2(['000001.SZ'], '1d', '20260105',
                                                    '20260105', incrementally=True)]
    for call in calls:
        with pytest.raises((QmtDataError, TypeError)):
            call()
    assert not list((rig[2].root / 'records').glob('*.json'))


@pytest.mark.parametrize('key,value', [('history_mode', 'bad'), ('download_timeout', 0),
    ('download_timeout', float('inf')), ('download_timeout', True), ('job_dir', '')])
def test_auto_config_rejects_invalid_values(key, value):
    with pytest.raises(ValueError):
        validate_config({key: value})


def test_relative_job_dir_follows_explicit_config(tmp_path):
    path = tmp_path / 'bridge.json'
    path.write_text(json.dumps({'bridge_dir': 'runtime', 'job_dir': 'jobs',
                               'history_mode': 'auto', 'download_timeout': 5}))
    config = load_config(path)
    assert config['bridge_dir'] == str((tmp_path / 'runtime').resolve())
    assert config['job_dir'] == str((tmp_path / 'jobs').resolve())


@pytest.mark.parametrize('shape', ['records', 'columns', 'positional', 'frame'])
def test_auto_original_time_shape_contract(shape):
    from bigqmt_bridge.kline import original_frames
    row = bars()[0]
    row['stime'] = '20260105150000'
    del row['time']
    if shape == 'columns':
        data = {key: [value] for key, value in row.items()}
    elif shape == 'positional':
        data = [list(row.values())]
    elif shape == 'frame':
        data = {'__frame__': True, 'columns': list(row), 'index': ['20260105150000'], 'data': [row]}
    else:
        data = [row]
    raw = {'__qmt_raw_market__': 1, 'fields': [key for key in row if key != 'stime'],
           'data': {'000001.SZ': data}}
    with pytest.raises(QmtDataError, match='time|UTC'):
        original_frames(raw, ['000001.SZ'])
