import gzip
import importlib.util
import json
import time

import pytest


def module():
    assert importlib.util.find_spec('bigqmt_bridge.realtime_collector'), 'realtime consumer missing'
    from bigqmt_bridge import realtime_collector
    return realtime_collector


class FakeConnection:
    stopped = False
    fail = False
    def __init__(self, *args, **kwargs):
        self.n = 0
        self.source_start = round(time.time()*1000)-1000
    def open(self):
        return self
    def close(self):
        self.stopped = True
    def call(self, kind, body=None):
        if kind == 'market_status':
            return {'schema': 'qmt-market-memory-v1', 'mode': 'poll_snapshot',
                    'generation': 'a'*32, 'incremental_complete': False,
                    'max_age_ms': 5000}
        if kind == 'market_snapshot':
            self.n += 1
            if self.fail and self.n > 1:
                raise TimeoutError('PRIVATE_QUOTE should not enter report')
            now = round(time.time()*1000)
            source = self.source_start+self.n
            return {'schema': 'qmt-market-memory-v1', 'mode': 'poll_snapshot',
                'generation': 'a'*32, 'incremental_complete': False, 'state': 'complete',
                'quotes': {'000001.SZ': {'code': '000001.SZ', 'bridge_sequence': self.n,
                    'received_at_utc_ms': now, 'source_time_utc_ms': source,
                    'source_age_ms': now-source, 'effective_source_age_ms': now-source,
                    'fresh': True, 'complete_frame': True, 'health': 'fresh',
                    'data': {'lastPrice': 20+self.n}}}}
        return {'schema': 'qmt-market-memory-v1', 'mode': 'poll_snapshot',
                'generation': 'a'*32, 'incremental_complete': False, 'state': 'complete', 'subscriptions':
                {'000001.SZ': {'state': 'unsubscribed'}} if kind == 'market_unsubscribe'
                else {'000001.SZ': {'state': 'active'}}}


def setup(monkeypatch, m):
    monkeypatch.setattr(m, 'load_config', lambda path: {'mode': 'market'})
    monkeypatch.setattr(m, 'Connection', FakeConnection)


def test_external_consumer_archives_received_quotes_only(tmp_path, monkeypatch):
    m = module(); setup(monkeypatch, m)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['state'] == 'complete'
    assert result['realtime_verified'] is True
    assert result['snapshots'] > 1
    assert result['incremental_complete'] is False
    path = tmp_path/'archive'/result['archive']['file']
    with gzip.open(path, 'rt') as stream:
        rows = [json.loads(line) for line in stream]
    assert len(rows) == result['snapshots']
    assert rows[0]['quotes']['000001.SZ']['data']['lastPrice'] == 21
    assert 'lastPrice' not in json.dumps(result)


def test_disconnect_preserves_partial_archive_and_sanitizes_error(tmp_path, monkeypatch):
    m = module(); setup(monkeypatch, m)
    monkeypatch.setattr(FakeConnection, 'fail', True)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['state'] == 'incomplete'
    assert result['bridge_health'] == 'disconnected'
    assert result['error'] == 'TimeoutError'
    assert result['snapshots'] == 1
    assert 'PRIVATE_QUOTE' not in json.dumps(result)


def test_memory_ipc_never_falls_back_to_output_files(tmp_path, monkeypatch):
    m = module(); setup(monkeypatch, m)
    monkeypatch.setattr(m, 'load_config', lambda path: {'mode': 'probe'})
    with pytest.raises(ValueError):
        m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive')
    assert not (tmp_path/'archive').exists()


def test_actual_incomplete_market_frames_cannot_complete_collection(tmp_path, monkeypatch):
    from qmt_bridge.market_memory_v1 import MarketApplication
    m = module(); setup(monkeypatch, m)

    class Context:
        def subscribe_quote(self, **kwargs):
            return 12
        def unsubscribe_quote(self, ident):
            assert ident == 12
        def get_full_tick(self, **kwargs):
            return {'000001.SZ': {'lastPrice': 20, 'time': round(time.time()*1000)}}

    class ActualApplicationConnection(FakeConnection):
        def __init__(self, *args, **kwargs):
            self.application = MarketApplication(Context())
        def call(self, kind, body=None):
            return self.application(kind, body or {})[1]

    monkeypatch.setattr(m, 'Connection', ActualApplicationConnection)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['state'] == 'incomplete'
    assert result['realtime_verified'] is False
    assert result['incomplete_snapshots'] > 0
    assert result['per_code']['000001.SZ']['complete_frames'] == 0
    assert result['quotes'] > 0  # User can inspect the explicitly incomplete archive.


def test_actual_global_sequences_follow_requested_codes_not_sorted_json_keys(tmp_path, monkeypatch):
    from qmt_bridge.market_memory_v1 import MarketApplication
    m = module(); setup(monkeypatch, m)
    codes = ['510300.SH', '000001.SZ']

    class Context:
        count = 0
        def subscribe_quote(self, **kwargs):
            self.count += 1
            return self.count
        def unsubscribe_quote(self, ident):
            pass
        def get_full_tick(self, stock_code):
            return {code: {'lastPrice': 20, 'volume': 10, 'amount': 200,
                'time': round(time.time()*1000)-1, 'askPrice': [20.1]*5,
                'bidPrice': [19.9]*5, 'askVol': [10]*5, 'bidVol': [10]*5}
                for code in stock_code}

    class ActualApplicationConnection(FakeConnection):
        def __init__(self, *args, **kwargs):
            self.application = MarketApplication(Context())
            self.application('market_snapshot', {'codes': codes})
        def call(self, kind, body=None):
            reply = self.application(kind, body or {})[1]
            return json.loads(json.dumps(reply, sort_keys=True))

    monkeypatch.setattr(m, 'Connection', ActualApplicationConnection)
    result = m.collect(tmp_path/'private'/'session.local.json', codes, tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['state'] == 'complete'
    assert result['quotes'] == 2*result['snapshots']
    with gzip.open(tmp_path/'archive'/result['archive']['file'], 'rt') as stream:
        first = json.loads(next(stream))
    assert first['quotes']['510300.SH']['bridge_sequence'] == 3
    assert first['quotes']['000001.SZ']['bridge_sequence'] == 4


@pytest.mark.parametrize('defect', ['missing_generation', 'generation_change', 'first_bool_sequence',
                                   'repeated_sequence', 'sequence_gap', 'wrong_code'])
def test_invalid_producer_identity_or_sequence_never_counts_as_complete(
        tmp_path, monkeypatch, defect):
    m = module(); setup(monkeypatch, m)

    class InvalidConnection(FakeConnection):
        def call(self, kind, body=None):
            reply = super().call(kind, body)
            if kind == 'market_snapshot':
                quote = reply['quotes']['000001.SZ']
                if defect == 'missing_generation':
                    reply.pop('generation')
                elif defect == 'generation_change' and self.n > 1:
                    reply['generation'] = 'b'*32
                elif defect == 'first_bool_sequence':
                    quote['bridge_sequence'] = True
                elif defect == 'repeated_sequence':
                    quote['bridge_sequence'] = 1
                elif defect == 'sequence_gap' and self.n > 1:
                    quote['bridge_sequence'] += 1
                elif defect == 'wrong_code':
                    quote['code'] = '510300.SH'
            return reply

    monkeypatch.setattr(m, 'Connection', InvalidConnection)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['state'] == 'incomplete'
    assert result['realtime_verified'] is False
    assert result['error'] == 'ValueError'
    if defect in ('missing_generation', 'first_bool_sequence', 'wrong_code'):
        assert result['snapshots'] == 0


def test_quote_age_is_rechecked_at_external_receipt(tmp_path, monkeypatch):
    m = module(); setup(monkeypatch, m)

    class DelayedQuoteConnection(FakeConnection):
        def call(self, kind, body=None):
            reply = super().call(kind, body)
            if kind == 'market_snapshot':
                reply['quotes']['000001.SZ']['source_time_utc_ms'] -= 60000
            return reply

    monkeypatch.setattr(m, 'Connection', DelayedQuoteConnection)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['fresh_quotes'] == 0
    assert result['realtime_verified'] is False
    assert result['per_code']['000001.SZ']['last_health'] == 'stale'


def test_previous_fresh_changes_do_not_hide_final_stale_health(tmp_path, monkeypatch):
    m = module(); setup(monkeypatch, m)

    class EventuallyStaleConnection(FakeConnection):
        def call(self, kind, body=None):
            reply = super().call(kind, body)
            if kind == 'market_snapshot' and self.n > 2:
                reply['quotes']['000001.SZ'].update(fresh=False, health='stale',
                    source_age_ms=6000, effective_source_age_ms=6000)
            return reply

    monkeypatch.setattr(m, 'Connection', EventuallyStaleConnection)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.04, interval=.002)
    assert result['fresh_quotes'] >= 2 and result['changed_quotes'] > 0
    assert result['realtime_verified'] is False
    assert result['per_code']['000001.SZ']['last_health'] == 'stale'


def test_delayed_close_never_reports_a_closed_bridge(tmp_path, monkeypatch):
    m = module(); setup(monkeypatch, m)

    class PendingCloseConnection(FakeConnection):
        def close(self):
            raise TimeoutError('cancellation not complete')

    monkeypatch.setattr(m, 'Connection', PendingCloseConnection)
    result = m.collect(tmp_path/'private'/'session.local.json', ['000001.SZ'], tmp_path/'archive',
                       duration=.03, interval=.001)
    assert result['close_pending'] is True
    assert result['state'] == 'incomplete'
    assert result['bridge_health'] != 'closed'
