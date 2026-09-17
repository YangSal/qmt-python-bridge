"""Read-only market application contract, with no terminal or file IPC."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    assert importlib.util.find_spec('qmt_bridge.market_memory_v1'), 'market memory application missing'
    from qmt_bridge import market_memory_v1
    return market_memory_v1


def tick(**changes):
    data = {'time': 1767596400000, 'lastPrice': 10., 'open': 9., 'high': 11.,
        'low': 8., 'lastClose': 9., 'volume': 100, 'amount': 1000.,
        'askPrice': [10.1, 10.2, 0., 0., 0.], 'bidPrice': [9.9, 9.8, 0., 0., 0.],
        'askVol': [10, 20, 0, 0, 0], 'bidVol': [30, 40, 0, 0, 0]}
    data.update(changes)
    return data


class Context:
    def __init__(self):
        self.ticks = {'000001.SZ': tick(), '510300.SH': tick()}
        self.subscribed, self.unsubscribed, self.reads = [], [], []
        self.fail_subscribe, self.fail_unsubscribe = set(), set()

    def get_full_tick(self, *, stock_code):
        self.reads.append(stock_code)
        return {c: self.ticks[c] for c in stock_code if c in self.ticks}

    def subscribe_quote(self, *, stock_code, period, dividend_type, result_type, callback):
        assert (period, dividend_type, result_type, callback) == ('tick', 'none', 'dict', None)
        self.subscribed.append(stock_code)
        if stock_code in self.fail_subscribe:
            raise RuntimeError('PRIVATE RAW PAYLOAD MUST NOT ESCAPE')
        return len(self.subscribed)

    def unsubscribe_quote(self, sub_id):
        self.unsubscribed.append(sub_id)
        if sub_id in self.fail_unsubscribe:
            raise RuntimeError('PRIVATE RAW PAYLOAD MUST NOT ESCAPE')


def application(context=None, **kwargs):
    return module().MarketApplication(context or Context(), wall_clock=lambda: 1767596401.,
                                      monotonic_clock=lambda: 10., **kwargs)


def test_snapshot_has_real_source_time_receive_time_sequence_and_original_empty_levels():
    context = Context()
    app = application(context)
    kind, reply = app('market_snapshot', {'codes': ['000001.SZ', '510300.SH']})
    assert kind == 'market_snapshot_reply' and reply['mode'] == 'poll_snapshot'
    assert reply['incremental_complete'] is False and reply['missed_updates'] is None
    quote = reply['quotes']['000001.SZ']
    assert quote['source_time_utc_ms'] == 1767596400000
    assert quote['received_at_utc_ms'] == 1767596401000
    assert quote['source_age_ms'] == 1000 and quote['fresh'] is True
    assert quote['bridge_sequence'] == 1 and reply['quotes']['510300.SH']['bridge_sequence'] == 2
    assert quote['data']['askPrice'] == [10.1, 10.2, 0., 0., 0.]
    assert quote['book_state'] == 'present' and quote['complete_frame'] is True
    context.ticks['000001.SZ']['askPrice'][0] = 50.
    assert quote['data']['askPrice'][0] == 10.1


@pytest.mark.parametrize('body', [{}, {'codes': []}, {'codes': ['000001.SZ'] * 2},
    {'codes': ['000001.HK']}, {'codes': ['../000001.SZ']},
    {'codes': ['%06d.SZ' % n for n in range(11)]},
    {'codes': ['000001.SZ'], 'account': 'not_allowed'}])
def test_invalid_scope_rejected_before_native_calls(body):
    context = Context()
    with pytest.raises(ValueError):
        application(context)('market_snapshot', body)
    assert context.reads == []


def test_subscribe_is_bounded_idempotent_and_does_not_claim_initial_snapshot():
    context = Context()
    app = application(context)
    _, reply = app('market_subscribe', {'codes': ['000001.SZ', '510300.SH']})
    assert reply['subscriptions']['000001.SZ']['subscription_id'] == 1
    assert reply['snapshot_required'] is True and context.subscribed == ['000001.SZ', '510300.SH']
    app('market_subscribe', {'codes': ['000001.SZ']})
    assert len(context.subscribed) == 2
    _, status = app('market_status', {})
    assert status['quotes']['000001.SZ']['health'] == 'not_observed'
    app('market_subscribe', {'codes': ['%06d.SZ' % n for n in range(2, 10)]})
    with pytest.raises(ValueError, match='capacity'):
        app('market_subscribe', {'codes': ['600000.SH']})
    assert len(context.subscribed) == 10


def test_unknown_subscription_does_not_blindly_issue_another_native_subscribe():
    context = Context()
    context.fail_subscribe.add('000001.SZ')
    app = application(context)
    _, reply = app('market_subscribe', {'codes': ['000001.SZ', '510300.SH']})
    assert reply['subscriptions']['000001.SZ']['state'] == 'unknown'
    assert reply['subscriptions']['510300.SH']['state'] == 'active'
    app('market_subscribe', {'codes': ['000001.SZ']})
    assert context.subscribed == ['000001.SZ', '510300.SH']
    assert 'PRIVATE' not in json.dumps(reply)


def test_unsubscribe_and_close_own_only_their_ids_and_clear_snapshot_generation():
    context = Context()
    app = application(context)
    app('market_subscribe', {'codes': ['000001.SZ', '510300.SH']})
    _, quote = app('market_snapshot', {'codes': ['000001.SZ']})
    generation = quote['generation']
    app('market_unsubscribe', {'codes': ['000001.SZ']})
    assert context.unsubscribed == [1]
    result = app.close()
    assert context.unsubscribed == [1, 2] and result['remaining_subscriptions'] == 0
    _, status = app('market_status', {})
    assert status['generation'] != generation and status['quotes'] == {}
    _, snapshot = app('market_snapshot', {'codes': ['000001.SZ']})
    assert snapshot['quotes']['000001.SZ']['bridge_sequence'] == 1


def test_cleanup_failure_is_retained_and_does_not_stop_other_cleanup():
    context = Context()
    context.fail_unsubscribe.add(1)
    app = application(context)
    app('market_subscribe', {'codes': ['000001.SZ', '510300.SH']})
    result = app.close()
    assert result['remaining_subscriptions'] == 1 and context.unsubscribed == [1, 2]
    _, status = app('market_status', {})
    assert status['subscriptions']['000001.SZ']['state'] == 'cleanup_failed'
    assert 'PRIVATE' not in json.dumps(status)


def test_missing_code_and_missing_book_are_explicit_and_not_filled():
    context = Context()
    del context.ticks['000001.SZ']['askVol']
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ', '600000.SH']})
    quote = reply['quotes']['000001.SZ']
    assert quote['book_state'] == 'missing' and quote['complete_frame'] is False and quote['fresh'] is False
    assert 'askVol' not in quote['data']
    assert reply['quotes']['600000.SH']['error'] == 'missing_code'


@pytest.mark.parametrize('changes', [{'lastPrice': float('nan')}, {'volume': True},
    {'askVol': [1, 2]}, {'bidPrice': [1, 2, 3, 4, '5']}, {'amount': -1}])
def test_invalid_native_values_do_not_become_usable_snapshots(changes):
    context = Context()
    context.ticks['000001.SZ'] = tick(**changes)
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ', '510300.SH']})
    assert reply['quotes']['000001.SZ']['fresh'] is False
    assert reply['quotes']['000001.SZ']['complete_frame'] is False
    assert reply['quotes']['510300.SH']['fresh'] is True
    json.dumps(reply, allow_nan=False)


def test_timetag_requires_explicit_zone_and_reports_configured_assumption():
    context = Context()
    context.ticks['000001.SZ'].pop('time')
    context.ticks['000001.SZ']['timetag'] = '20260105 15:00:00'
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['source_time_utc_ms'] is None
    assert reply['quotes']['000001.SZ']['health'] == 'time_unknown'
    _, reply = application(context, timetag_timezone='Asia/Shanghai')('market_snapshot', {'codes': ['000001.SZ']})
    quote = reply['quotes']['000001.SZ']
    assert quote['source_time_utc_ms'] == 1767596400000 and quote['source_age_ms'] == 1000
    assert quote['source_time_basis'] == 'configured_timetag_timezone'
    assert quote['timetag_timezone'] == 'Asia/Shanghai' and quote['data']['timetag'] == '20260105 15:00:00'


def test_stale_future_and_backward_source_times_are_not_fresh():
    context = Context()
    app = application(context)
    app('market_snapshot', {'codes': ['000001.SZ']})
    context.ticks['000001.SZ']['time'] -= 1000
    _, reply = app('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['health'] == 'out_of_order'
    context.ticks['000001.SZ']['time'] = 1767596390000
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['health'] == 'stale'
    context.ticks['000001.SZ']['time'] = 1767596410000
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['health'] == 'clock_skew'


def test_status_recalculates_age_without_market_payload_or_another_native_read():
    context = Context()
    now, mono = [1767596401.], [10.]
    app = module().MarketApplication(context, wall_clock=lambda: now[0], monotonic_clock=lambda: mono[0])
    app('market_snapshot', {'codes': ['000001.SZ']})
    now[0], mono[0] = 1767596410., 19.
    _, reply = app('market_health', {})
    assert reply['quotes']['000001.SZ']['source_age_ms'] == 10000
    assert reply['quotes']['000001.SZ']['last_receive_age_ms'] == 9000
    assert reply['quotes']['000001.SZ']['health'] == 'stale'
    assert 'askPrice' not in json.dumps(reply) and len(context.reads) == 1


def test_large_nested_payload_is_rejected_without_oversize_transport_response():
    context = Context()
    context.ticks['000001.SZ']['unknown'] = ['x' * 1000] * 1000
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['error'] == 'invalid_native_payload'
    assert len(json.dumps(reply).encode('ascii')) < 262000


def test_numpy_numeric_scalars_are_converted_without_numpy_dependency_inside_qmt():
    import numpy as np
    context = Context()
    context.ticks['000001.SZ'].update(time=np.int64(1767596400000), lastPrice=np.float64(10.), volume=np.int64(100))
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['fresh'] is True
    json.dumps(reply, allow_nan=False)


def test_python36_syntax_no_disk_payload_or_native_error_message(capsys, monkeypatch):
    context = Context()
    def fail(**kwargs):
        raise ValueError('SECRET MARKET PAYLOAD')
    context.get_full_tick = fail
    m = module()
    ast.parse(Path(m.__file__).read_text('ascii'), feature_version=(3, 6))
    def forbidden(*args, **kwargs):
        raise AssertionError('market application must not write files')
    monkeypatch.setattr('builtins.open', forbidden)
    _, reply = application(context)('market_snapshot', {'codes': ['000001.SZ']})
    assert reply['quotes']['000001.SZ']['error'] == 'native_get_full_tick_ValueError'
    assert 'SECRET' not in json.dumps(reply) and capsys.readouterr().out == ''


def test_future_timestamp_cannot_poison_ordering_of_later_valid_snapshot():
    context = Context()
    context.ticks['000001.SZ']['time'] = 1767596490000
    app = application(context)
    assert app('market_snapshot', {'codes': ['000001.SZ']})[1]['quotes']['000001.SZ']['fresh'] is False
    context.ticks['000001.SZ']['time'] = 1767596400000
    quote = app('market_snapshot', {'codes': ['000001.SZ']})[1]['quotes']['000001.SZ']
    assert quote['fresh'] is True and quote['out_of_order'] is False


def test_monotonic_receive_age_prevents_freshness_when_wall_clock_stalls():
    mono = [10.]
    app = module().MarketApplication(Context(), wall_clock=lambda: 1767596401., monotonic_clock=lambda: mono[0])
    app('market_snapshot', {'codes': ['000001.SZ']})
    mono[0] = 30.
    quote = app('market_health', {})[1]['quotes']['000001.SZ']
    assert quote['last_receive_age_ms'] == 20000 and quote['fresh'] is False


@pytest.mark.parametrize('returned', [False, 0, 'failed'])
def test_invalid_unsubscribe_result_keeps_id_until_a_known_return(returned):
    context = Context()
    app = application(context)
    app('market_subscribe', {'codes': ['000001.SZ']})
    original = context.unsubscribe_quote
    context.unsubscribe_quote = lambda sub_id: returned
    assert app.close()['remaining_subscriptions'] == 1
    context.unsubscribe_quote = original
    assert app.close()['remaining_subscriptions'] == 0
    assert context.unsubscribed == [1]


def test_source_age_and_elapsed_receive_time_combine_when_wall_clock_stalls():
    mono = [10.]
    app = module().MarketApplication(Context(), wall_clock=lambda: 1767596404.5, monotonic_clock=lambda: mono[0])
    app('market_snapshot', {'codes': ['000001.SZ']})
    mono[0] = 11.
    quote = app('market_health', {})[1]['quotes']['000001.SZ']
    assert quote['source_age_ms'] == 4500
    assert quote['effective_source_age_ms'] == 5500 and quote['fresh'] is False


def test_duplicate_polls_cannot_renew_old_source_data_with_stalled_wall_clock():
    mono = [10.]
    app = module().MarketApplication(Context(), wall_clock=lambda: 1767596404.5, monotonic_clock=lambda: mono[0])
    app('market_snapshot', {'codes': ['000001.SZ']})
    mono[0] = 30.
    quote = app('market_snapshot', {'codes': ['000001.SZ']})[1]['quotes']['000001.SZ']
    assert quote['duplicate'] is True and quote['effective_source_age_ms'] == 24500 and quote['fresh'] is False


def test_timetag_parser_does_not_require_lazy_strptime_or_thread_module(monkeypatch):
    context = Context()
    context.ticks['000001.SZ'].pop('time')
    context.ticks['000001.SZ']['timetag'] = '20260105 15:00:00.125'
    app = application(context, timetag_timezone='Asia/Shanghai')
    import sys
    monkeypatch.setitem(sys.modules, '_strptime', None)
    quote = app('market_snapshot', {'codes': ['000001.SZ']})[1]['quotes']['000001.SZ']
    assert quote['source_time_utc_ms'] == 1767596400125


def test_small_monotonic_intervals_accumulate_without_per_poll_rounding_loss():
    mono = [10.]
    app = module().MarketApplication(Context(), wall_clock=lambda: 1767596404.5, monotonic_clock=lambda: mono[0])
    app('market_snapshot', {'codes': ['000001.SZ']})
    for index in range(1, 6):
        mono[0] = 10. + .0004 * index
        quote = app('market_health', {})[1]['quotes']['000001.SZ']
    assert quote['effective_source_age_ms'] == 4502
