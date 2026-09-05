import pandas as pd
import pytest


def test_divid_accepts_pre_2001_milliseconds_without_changing_calendar_dates():
    from bigqmt_bridge.normalize import beijing_date, normalize_divid
    assert beijing_date('673113600000') == pd.Timestamp('1991-05-02')
    assert beijing_date('19910501') == pd.Timestamp('1991-05-01')
    frame = normalize_divid({'673113600000': [0, 0, 0, 0, 0, 0, 1]}, '19910502', '19910502')
    assert len(frame) == 1


def test_raw_market_envelope_reconstructs_positional_rows_outside_qmt():
    from bigqmt_bridge.normalize import normalize_market
    raw = {'__qmt_raw_market__': 1, 'fields': ['open', 'close', 'volume'],
           'data': {'000001.SZ': [['20260903150000', 10, 11, 9007199254740993]]}}
    row = normalize_market(raw, ['000001.SZ'])['000001.SZ'].iloc[0]
    assert row['time'] == 1788418800000
    assert row['open'] == 10 and row['close'] == 11
    assert row['volume'] == 9007199254740993


def test_pre_2001_native_market_ms_and_raw_empty_remain_distinct():
    from bigqmt_bridge.normalize import normalize_market
    from bigqmt_bridge import QmtDataError
    f = normalize_market({'a': [{'time': 673113600000, 'close': 1}]}, ['a'])['a']
    assert f.time.iloc[0] == 673113600000
    with pytest.raises(QmtDataError):
        normalize_market({'__qmt_raw_market__': 1, 'fields': ['close'], 'data': {'a': []}}, ['a'])


@pytest.mark.parametrize('rows', [
    [{'stime': '20260903', 'close': 1}],
    [['20260903', 1, 2], ['20260903', 1]],
    {'stime': ['20260903'], 'close': [1]},
])
def test_raw_missing_fields_are_not_created_as_nan(rows):
    from bigqmt_bridge.normalize import normalize_market
    from bigqmt_bridge import QmtDataError
    raw = {'__qmt_raw_market__': 1, 'fields': ['close', 'volume'], 'data': {'a': rows}}
    with pytest.raises(QmtDataError):
        normalize_market(raw, ['a'])


def test_market_time_and_book_survive_normalization():
    from bigqmt_bridge.normalize import normalize_market
    raw = {'000001.SZ': [{'stime': '20260903150000', 'lastPrice': 10,
                         'askPrice': [10, 11, 12, 13, 14], 'volume': 9007199254740993}]}
    df = normalize_market(raw, ['000001.SZ'])['000001.SZ']
    assert pd.to_datetime(df.time.iloc[0], unit='ms', utc=True) == pd.Timestamp('2026-09-03T07:00:00Z')
    assert df.askPrice.iloc[0] == [10, 11, 12, 13, 14]
    assert df.volume.iloc[0] == 9007199254740993


def test_empty_or_missing_code_is_failure():
    from bigqmt_bridge.normalize import normalize_market
    from bigqmt_bridge.backend import QmtDataError
    for raw in ({}, {'000001.SZ': []}, {'000002.SZ': [{'time': 1}]}):
        with pytest.raises(QmtDataError):
            normalize_market(raw, ['000001.SZ'])


def test_divid_dates_are_beijing_and_range_is_inclusive():
    from bigqmt_bridge.normalize import normalize_divid
    raw = {'1626796800000': [0.48, 0, 0, 0, 0, 0, 1.05],
           '1658332800000': [0.41, 0, 0, 0, 0, 0, 1.06]}
    df = normalize_divid(raw, '20210721', '20210721')
    assert list(df.index) == [pd.Timestamp('2021-07-21')]
    assert df.iloc[0].interest == .48
    assert df.iloc[0].dr == 1.05
    assert normalize_divid({}, '20210721', '20210721').empty


def test_backend_batches_and_enforces_market_contract():
    from bigqmt_bridge.backend import InnerBackend, QmtDataError
    class Transport:
        def call(self, op, args):
            assert op == 'market_data'
            assert args['subscribe'] is False and args['fill_data'] is False
            assert len(args['stock_list']) <= 2
            return {c: [{'time': 1788418800000, 'close': 10}] for c in args['stock_list']}
    backend = InnerBackend(Transport(), {'batch_size': 2, 'cache_prepared': True})
    assert len(backend.get_market_data_ex(['time', 'close'], ['a', 'b', 'c'], period='1d')) == 3
    with pytest.raises(QmtDataError):
        backend.get_market_data_ex([], ['a'], subscribe=True)


def test_cache_ack_and_complete_instrument_do_not_silently_downgrade():
    from bigqmt_bridge.backend import InnerBackend, QmtDataError
    class Transport:
        def call(self, op, args):
            return {'InstrumentName': 'test'}
    backend = InnerBackend(Transport(), {})
    with pytest.raises(QmtDataError):
        backend.download_history_data2(['a'], '1m')
    with pytest.raises(QmtDataError):
        backend.get_instrument_detail('a', iscomplete=True)
    backend = InnerBackend(Transport(), {'instrument_fields': ['InstrumentName', 'TotalVolume']})
    with pytest.raises(QmtDataError):
        backend.get_instrument_detail('a', iscomplete=True)


def test_financial_raw_field_dates_and_missing_identity():
    from bigqmt_bridge.normalize import normalize_financial
    from bigqmt_bridge.backend import QmtDataError
    raw = {'000001.SZ': {'ASHAREINCOME.m_timetag': {'1': 20260630},
                        'ASHAREINCOME.m_anntime': {'1': 20260801},
                        'ASHAREINCOME.revenue': {'1': 120}}}
    df = normalize_financial(raw, '000001.SZ', 'Income', 'ASHAREINCOME',
                             ['m_timetag', 'm_anntime', 'revenue'])
    assert df.to_dict('records') == [{'m_timetag': '20260630', 'm_anntime': '20260801', 'revenue': 120}]
    del raw['000001.SZ']['ASHAREINCOME.m_anntime']
    with pytest.raises(QmtDataError):
        normalize_financial(raw, '000001.SZ', 'Income', 'ASHAREINCOME', ['m_timetag', 'm_anntime', 'revenue'])



def test_financial_schema_covers_existing_eight_tables():
    from bigqmt_bridge.normalize import financial_schema
    schema = financial_schema()
    assert len(schema) == 8
    assert 'm_anntime' in schema['Income']['fields']
    assert 'rank' in schema['Top10holder']['fields']
    assert schema['Pershareindex']['prefix'] == 'PERSHAREINDEX'



def test_tick_book_is_not_silently_replaced_by_nulls():
    from bigqmt_bridge.backend import InnerBackend, QmtDataError
    class T:
        def call(self, op, args):
            return {'a': [{'time': 1788418800000, 'lastPrice': 1, 'volume': 1, 'amount': 1,
                           'askPrice': '1,2,3', 'bidPrice': [], 'askVol': [1], 'bidVol': [1]}]}
    with pytest.raises(QmtDataError, match='five-level'):
        InnerBackend(T(), {'cache_prepared': True}).get_market_data_ex([], ['a'], period='tick')


def test_empty_sector_cache_must_not_look_like_removed_members():
    from bigqmt_bridge.backend import InnerBackend, QmtDataError
    class T:
        def call(self, *args):
            return []
    with pytest.raises(QmtDataError):
        InnerBackend(T(), {}).get_stock_list_in_sector('sector')


@pytest.mark.parametrize('op,raw', [('market', {'a': {'__frame__': True}}),
                                  ('divid', {'nonsense': [0]*7})])
def test_malformed_wire_errors_are_always_bridge_errors(op, raw):
    from bigqmt_bridge.backend import InnerBackend, QmtDataError
    class T:
        def call(self, *args):
            return raw
    backend = InnerBackend(T(), {'cache_prepared': True})
    with pytest.raises(QmtDataError):
        if op == 'market':
            backend.get_market_data_ex([], ['a'])
        else:
            backend.get_divid_factors('a')


def test_financial_sparse_rank_is_rejected_before_job_can_drop_rows():
    from bigqmt_bridge.normalize import normalize_financial
    from bigqmt_bridge import QmtDataError
    raw = {'a': {'TOP10HOLDER.endDate': {'x': 20260331, 'y': 20260630},
                 'TOP10HOLDER.declareDate': {'x': 20260430, 'y': 20260801},
                 'TOP10HOLDER.rank': {'x': 1}}}
    with pytest.raises(QmtDataError, match='rank'):
        normalize_financial(raw, 'a', 'Top10holder', 'TOP10HOLDER', ['endDate', 'declareDate', 'rank'])


def test_tick_missing_persisted_scalar_is_rejected():
    from bigqmt_bridge.backend import InnerBackend, QmtDataError
    class T:
        def call(self, *args):
            row = {'time': 1788418800000, 'lastPrice': 1, 'volume': 1, 'amount': 1}
            row.update({k: [1]*5 for k in ('askPrice', 'bidPrice', 'askVol', 'bidVol')})
            return {'a': [row]}
    with pytest.raises(QmtDataError, match='scalar'):
        InnerBackend(T(), {'cache_prepared': True}).get_market_data_ex([], ['a'], period='tick')



def test_financial_conflicting_identity_is_not_silently_deduplicated():
    from bigqmt_bridge.normalize import normalize_financial
    from bigqmt_bridge import QmtDataError
    frame = pd.DataFrame({'m_timetag': [20260630]*2, 'm_anntime': [20260801]*2, 'revenue': [1, 2]})
    with pytest.raises(QmtDataError, match='conflicting'):
        normalize_financial({'a': frame}, 'a', 'Income', 'ASHAREINCOME', list(frame.columns))
