import copy

import pytest


def test_native_sample_uses_local_cache_without_subscribe_argument():
    from bigqmt_bridge.cli import collect_sample
    import pandas as pd
    class Native:
        def get_market_data_ex(self, **kwargs):
            raise AssertionError('must use local cache, not generic market reader')
        def get_local_data(self, field_list, stock_list, period, start_time, end_time,
                           count, dividend_type, fill_data):
            assert fill_data is False
            return {'a': pd.DataFrame([{'time': 1788418800000, 'open': 1, 'high': 2,
                'low': 1, 'close': 2, 'volume': 10, 'amount': 20}])}
    r = collect_sample(Native(), '20260903', ['a'], ['1m'], ['market'], backend='native')
    assert r['errors'] == []
    assert r['samples']['market']['1m']['a']['rows']


def sample():
    return {'version': 1, 'date': '20260903', 'codes': ['000001.SZ'],
            'periods': ['1m'], 'families': ['market'], 'errors': [],
            'samples': {'market': {'1m': {'000001.SZ': {
                'columns': ['time', 'close', 'volume'],
                'rows': [[1788418800000, 10., 123]]}}}}}


@pytest.mark.parametrize('change', ['column', 'date', 'volume', 'empty', 'error', 'code'])
def test_compare_rejects_incomplete_or_different_samples(change):
    from bigqmt_bridge.cli import compare
    left, right = sample(), sample()
    frame = right['samples']['market']['1m']['000001.SZ']
    if change == 'column':
        frame['columns'].pop()
    elif change == 'date':
        frame['rows'][0][0] += 86400000
    elif change == 'volume':
        frame['rows'][0][2] += 1
    elif change == 'empty':
        left['samples'] = right['samples'] = {}
    elif change == 'error':
        right['errors'] = ['timeout']
    else:
        right['codes'] = ['510300.SH']
    assert compare(left, right)


def test_compare_same_nonempty_samples_and_array_differences():
    from bigqmt_bridge.cli import compare
    left = sample()
    left['samples']['market']['1m']['000001.SZ']['rows'][0].append([1, 2, 3, 4, 5])
    left['samples']['market']['1m']['000001.SZ']['columns'].append('askPrice')
    right = copy.deepcopy(left)
    right['backend'] = 'file_bridge'
    assert compare(left, right) == []
    right['samples']['market']['1m']['000001.SZ']['rows'][0][-1][4] = 6
    assert compare(left, right)


def test_probe_missing_worker_returns_nonzero_with_report(tmp_path):
    from bigqmt_bridge.cli import main
    output = tmp_path / 'probe.json'
    assert main(['probe', '--backend', 'file_bridge', '--bridge-dir', str(tmp_path / 'bridge'),
                 '--timeout', '0.05', '--output', str(output)]) == 1
    assert 'timeout' in output.read_text()


def test_sample_does_not_download_or_write_db():
    from bigqmt_bridge.cli import collect_sample
    import pandas as pd
    class Source:
        def get_market_data_ex(self, **kw):
            assert kw['subscribe'] is False and kw['fill_data'] is False
            assert kw['start_time'] == kw['end_time'] == '20260903'
            return {'000001.SZ': pd.DataFrame([{'time': 1788418800000,
                'open': 9, 'high': 11, 'low': 8, 'close': 10, 'volume': 123, 'amount': 1230}])}
    report = collect_sample(Source(), '20260903', ['000001.SZ'], ['1m'], ['market'])
    assert report['errors'] == []
    assert report['samples']['market']['1m']['000001.SZ']['rows']
