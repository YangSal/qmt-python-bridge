import copy

import pandas as pd
import pytest

from bigqmt_bridge import QmtDataError
from bigqmt_bridge.backend import InnerBackend
from bigqmt_bridge.cli import collect_sample, compare


def row(value):
    return dict(time=1788418800000, open=10., high=11., low=9., close=value, volume=0, amount=0.)


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), True, 'invalid'])
def test_client_rejects_invalid_core_market_values(value):
    class Transport:
        def call(self, operation, args):
            return {'000001.SZ': [row(value)]}
    source = InnerBackend(Transport(), {'cache_prepared': True})
    with pytest.raises(QmtDataError):
        source.get_market_data_ex([], ['000001.SZ'], period='1d')


def test_native_sample_rejects_null_core_values_without_relying_on_bridge_backend():
    class Native:
        def get_local_data(self, **kwargs):
            return {'000001.SZ': pd.DataFrame([row(None)])}
    report = collect_sample(Native(), '20260903', ['000001.SZ'], ['1d'], ['market'], backend='native')
    assert report['errors']


def test_compare_rejects_two_identically_invalid_market_reports():
    report = {'version': 1, 'date': '20260903', 'codes': ['000001.SZ'], 'periods': ['1d'],
              'families': ['market'], 'errors': [], 'samples': {'market': {'1d': {'000001.SZ': {
                  'columns': ['time', 'open', 'high', 'low', 'close', 'volume', 'amount'],
                  'rows': [[1788418800000, None, None, None, None, None, None]]}}}}}
    assert compare(report, copy.deepcopy(report))


def test_zero_volume_is_valid_and_not_rewritten():
    class Transport:
        def call(self, operation, args):
            return {'000001.SZ': [row(10.)]}
    frame = InnerBackend(Transport(), {'cache_prepared': True}).get_market_data_ex([], ['000001.SZ'])['000001.SZ']
    assert frame['volume'].tolist() == [0]
    assert frame['amount'].tolist() == [0.]


@pytest.mark.parametrize('shape', ['columns', 'records'])
def test_raw_market_keeps_utc_time_when_terminal_stime_is_local(shape):
    from bigqmt_bridge.normalize import normalize_market
    # Synthetic values; the two times denote Sep 8 Beijing / Sep 7 Los Angeles.
    record = dict(time=1788796800000, stime='20260907', close=10., volume=100)
    rows = {k: [v] for k, v in record.items()} if shape == 'columns' else [record]
    raw = {'__qmt_raw_market__': 1, 'fields': ['close', 'volume'],
           'data': {'000001.SZ': rows}}
    frame = normalize_market(raw, ['000001.SZ'])['000001.SZ']
    assert frame['time'].tolist() == [1788796800000]
    assert frame['close'].tolist() == [10.]


def test_invalid_raw_utc_time_does_not_fall_back_to_plausible_stime():
    from bigqmt_bridge.normalize import normalize_market
    raw = {'__qmt_raw_market__': 1, 'fields': ['close'],
           'data': {'000001.SZ': {'time': [None], 'stime': ['20260908'], 'close': [10.]}}}
    with pytest.raises(QmtDataError):
        normalize_market(raw, ['000001.SZ'])
