# coding: ascii
"""Read-only QMT market application; called only by the owning timer thread.

No callback queues are enabled: their thread/lock contract is not qualified in
the embedded runtime. Native subscriptions use callback=None, and each explicit
snapshot request polls the native latest-value cache. Intermediate updates and
source sequence gaps are unobservable. No file I/O or payload logging occurs.
"""
import datetime
import json
import math
import os
import re
import time
from collections import OrderedDict
from numbers import Integral, Real


BOOK_FIELDS = ('askPrice', 'bidPrice', 'askVol', 'bidVol')
CORE_FIELDS = ('lastPrice', 'volume', 'amount')


def _codes(body):
    if not isinstance(body, dict) or set(body) != {'codes'}:
        raise ValueError('market request requires codes only')
    codes = body['codes']
    if (not isinstance(codes, list) or not 1 <= len(codes) <= 10 or
            any(not isinstance(c, str) or not re.match(r'^[0-9]{6}\.(SH|SZ|BJ)\Z', c) for c in codes) or
            len(set(codes)) != len(codes)):
        raise ValueError('market request requires 1..10 unique SH/SZ/BJ codes')
    return list(codes)


def _number(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _copy_value(value, depth=0, budget=None):
    """Bounded stdlib conversion including native numpy numeric scalars."""
    if budget is None:
        budget = [1024]
    budget[0] -= 1
    if depth > 4 or budget[0] < 0:
        raise ValueError('native payload structural bound exceeded')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > 256:
            raise ValueError('native string bound exceeded')
        return value
    if isinstance(value, Integral):
        result = int(value)
        if abs(result) > 2 ** 53:
            raise ValueError('native integer bound exceeded')
        return result
    if isinstance(value, Real):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite native value')
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise ValueError('native list bound exceeded')
        return [_copy_value(v, depth + 1, budget) for v in value]
    if isinstance(value, dict):
        if len(value) > 64 or any(not isinstance(k, str) or not 1 <= len(k) <= 64 for k in value):
            raise ValueError('native mapping bound exceeded')
        return {k: _copy_value(v, depth + 1, budget) for k, v in value.items()}
    raise ValueError('unsupported native value type')


class MarketApplication(object):
    """Single-owner application callable for MemoryWorker, usable after close().

    The owner must call close() on disconnect/stop to discard quote validity and
    retire subscriptions. Failed cleanups stay visible and can be retried with
    the same ID. close() is synchronous and cannot interrupt a native QMT call.
    """
    def __init__(self, context, max_age_ms=5000, timetag_timezone=None,
                 wall_clock=None, monotonic_clock=None):
        if not _number(max_age_ms) or not 0 < max_age_ms <= 86400000:
            raise ValueError('max_age_ms must be in (0, 86400000]')
        if timetag_timezone not in (None, 'Asia/Shanghai', '+08:00', 'UTC'):
            raise ValueError('unsupported explicit timetag timezone')
        self.context = context
        self.max_age_ms = max_age_ms
        self.timetag_timezone = timetag_timezone
        self.wall_clock = wall_clock or time.time
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.generation = os.urandom(16).hex()
        self.sequence = 0
        self.subscriptions = OrderedDict()
        self.observations = OrderedDict()
        self._effective_utc_ms = None
        self._clock_monotonic = None

    def _base(self):
        return {'schema': 'qmt-market-memory-v1', 'mode': 'poll_snapshot',
                'generation': self.generation, 'incremental_complete': False,
                'missed_updates': None, 'source_sequence_available': False,
                'trading_ready': False}

    def _clock(self):
        now_ms, monotonic_now = int(self.wall_clock() * 1000), self.monotonic_clock()
        elapsed_ms = (max(0, int(round((monotonic_now - self._clock_monotonic) * 1000)))
                      if self._clock_monotonic is not None else 0)
        inferred = self._effective_utc_ms + elapsed_ms if self._effective_utc_ms is not None else now_ms
        if self._effective_utc_ms is None or now_ms > inferred:
            self._effective_utc_ms = now_ms
            self._clock_monotonic = monotonic_now
        return now_ms, monotonic_now, max(now_ms, inferred)

    def _source_time(self, data):
        if 'time' in data:
            stamp = data['time']
            if type(stamp) is int and 946684800000 <= stamp < 4102444800000:
                return stamp, 'native_utc_ms'
            return None, 'unknown'
        tag = data.get('timetag')
        if self.timetag_timezone is None or not isinstance(tag, str):
            return None, 'unknown'
        zone = datetime.timezone.utc if self.timetag_timezone == 'UTC' else datetime.timezone(
            datetime.timedelta(hours=8))
        patterns = (r'^([0-9]{4})([0-9]{2})([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?\Z',
                    r'^([0-9]{4})([0-9]{2})([0-9]{2})([0-9]{2})([0-9]{2})([0-9]{2})(?:\.([0-9]{1,6}))?\Z')
        for pattern in patterns:
            match = re.match(pattern, tag)
            if match is None:
                continue
            try:
                parts = [int(v) for v in match.groups()[:6]]
                if not 2000 <= parts[0] < 2100:
                    return None, 'unknown'
                micros = int((match.group(7) or '').ljust(6, '0'))
                stamp = datetime.datetime(*parts, microsecond=micros, tzinfo=zone)
                return int(stamp.timestamp() * 1000), 'configured_timetag_timezone'
            except (ValueError, OverflowError, OSError):
                pass
        return None, 'unknown'

    def _health(self, quote, now_ms, monotonic_now=None, observed_at=None, effective_now_ms=None):
        source = quote.get('source_time_utc_ms')
        quote['source_age_ms'] = None if source is None else now_ms - source
        quote['effective_source_age_ms'] = (None if source is None else
            (now_ms if effective_now_ms is None else effective_now_ms) - source)
        if monotonic_now is not None and observed_at is not None:
            quote['last_receive_age_ms'] = max(0, int((monotonic_now - observed_at) * 1000))
        if quote.get('error'):
            health = 'unavailable'
        elif not quote.get('complete_frame'):
            health = 'incomplete'
        elif quote.get('out_of_order'):
            health = 'out_of_order'
        elif quote.get('zero_price'):
            health = 'zero_price'
        elif source is None:
            health = 'time_unknown'
        elif quote['source_age_ms'] < 0:
            health = 'clock_skew'
        elif quote['effective_source_age_ms'] > self.max_age_ms or quote.get('last_receive_age_ms', 0) > self.max_age_ms:
            health = 'stale'
        else:
            health = 'fresh'
        quote.update(health=health, fresh=health == 'fresh')
        return quote

    def _empty(self, code, now_ms):
        self.sequence += 1
        return {'code': code, 'bridge_sequence': self.sequence,
                'source_time_utc_ms': None, 'source_time_basis': 'unknown',
                'source_sequence': None, 'timetag_timezone': self.timetag_timezone,
                'received_at_utc_ms': now_ms, 'source_age_ms': None,
                'last_receive_age_ms': 0, 'complete_frame': False,
                'book_state': 'missing', 'missing_fields': [],
                'out_of_order': False, 'duplicate': False, 'fresh': False}

    def _quote(self, code, raw, now_ms):
        quote = self._empty(code, now_ms)
        try:
            if not isinstance(raw, dict) or not raw:
                raise ValueError('invalid native quote shape')
            data = _copy_value(raw)
            # Independent per-quote cap leaves room for ten quotes and envelope.
            if len(json.dumps(data, ensure_ascii=True, allow_nan=False).encode('ascii')) > 16000:
                raise ValueError('native quote byte bound exceeded')
        except Exception:
            quote['error'] = 'invalid_native_payload'
            return self._health(quote, now_ms)
        quote['data'] = data
        missing = [field for field in CORE_FIELDS + BOOK_FIELDS if field not in data]
        invalid_core = any(field in data and (not _number(data[field]) or data[field] < 0)
                           for field in CORE_FIELDS)
        invalid_book = any(field in data and (not isinstance(data[field], list) or
            len(data[field]) != 5 or any(not _number(v) or v < 0 for v in data[field])) for field in BOOK_FIELDS)
        quote['book_state'] = ('missing' if any(f not in data for f in BOOK_FIELDS) else
                               'invalid' if invalid_book else 'present')
        quote['missing_fields'] = missing
        quote['complete_frame'] = not missing and not invalid_core and not invalid_book
        quote['zero_price'] = data.get('lastPrice') == 0
        source, basis = self._source_time(data)
        quote.update(source_time_utc_ms=source, source_time_basis=basis)
        previous = self.observations.get(code)
        if previous is not None:
            high = previous['source_high_watermark']
            quote['out_of_order'] = source is not None and high is not None and source < high
            quote['duplicate'] = previous['fingerprint'] == json.dumps(data, sort_keys=True, ensure_ascii=True)
        return self._health(quote, now_ms)

    def _remember(self, code, quote, observed_at):
        previous = self.observations.pop(code, None)
        source = quote.get('source_time_utc_ms')
        high = previous['source_high_watermark'] if previous is not None else None
        if source is not None and quote['complete_frame'] and quote.get('source_age_ms', -1) >= 0:
            high = source if high is None else max(high, source)
        metadata = {k: v for k, v in quote.items() if k != 'data'}
        self.observations[code] = {'metadata': metadata, 'observed_at': observed_at,
            'source_high_watermark': high,
            'fingerprint': json.dumps(quote['data'], sort_keys=True, ensure_ascii=True) if 'data' in quote else None}
        while len(self.observations) > 10:
            self.observations.popitem(last=False)

    def _snapshot(self, codes):
        native_error = None
        try:
            raw = self.context.get_full_tick(stock_code=codes)
        except Exception as exc:
            raw = None
            native_error = 'native_get_full_tick_' + type(exc).__name__
        now_ms, observed_at, effective_now_ms = self._clock()
        quotes = {}
        for code in codes:
            if native_error or not isinstance(raw, dict) or code not in raw:
                quote = self._empty(code, now_ms)
                quote['error'] = native_error or ('invalid_native_result' if not isinstance(raw, dict) else 'missing_code')
                self._health(quote, now_ms)
            else:
                quote = self._quote(code, raw[code], now_ms)
            self._health(quote, now_ms, effective_now_ms=effective_now_ms)
            quotes[code] = quote
            self._remember(code, quote, observed_at)
        reply = self._base()
        reply.update(quotes=quotes, received_at_utc_ms=now_ms,
                     state='complete' if all(q['complete_frame'] for q in quotes.values()) else 'incomplete')
        return reply

    def _subscribe(self, codes):
        if len(set(self.subscriptions) | set(codes)) > 10:
            raise ValueError('market subscription capacity is 10 codes')
        result = {}
        for code in codes:
            if code not in self.subscriptions:
                item = {'code': code, 'subscription_id': None, 'state': 'unknown', 'error': None}
                # Preserve uncertain attempts in memory before entering native code.
                self.subscriptions[code] = item
                try:
                    sub_id = self.context.subscribe_quote(stock_code=code, period='tick',
                        dividend_type='none', result_type='dict', callback=None)
                    if not isinstance(sub_id, Integral) or isinstance(sub_id, bool) or not 0 < sub_id <= 2 ** 53:
                        item['error'] = 'invalid_subscription_id'
                    elif any(other is not item and other['subscription_id'] == int(sub_id)
                             for other in self.subscriptions.values()):
                        item['error'] = 'duplicate_subscription_id'
                    else:
                        item.update(subscription_id=int(sub_id), state='active')
                except Exception as exc:
                    item['error'] = 'native_subscribe_' + type(exc).__name__
            result[code] = dict(self.subscriptions[code])
        reply = self._base()
        reply.update(subscriptions=result, snapshot_required=True,
                     state='complete' if all(s['state'] == 'active' for s in result.values()) else 'incomplete')
        return reply

    def _unsubscribe(self, codes):
        result = {}
        for code in codes:
            item = self.subscriptions.get(code)
            self.observations.pop(code, None)
            if item is None:
                result[code] = {'code': code, 'subscription_id': None, 'state': 'not_subscribed', 'error': None}
                continue
            if item['subscription_id'] is None:
                item['error'] = item['error'] or 'unknown_subscription_id'
            else:
                try:
                    returned = self.context.unsubscribe_quote(item['subscription_id'])
                    if returned is not None and returned is not True:
                        item.update(state='cleanup_failed', error='native_unsubscribe_unconfirmed_result')
                    else:
                        item.update(state='unsubscribed', error=None)
                        del self.subscriptions[code]
                except Exception as exc:
                    item.update(state='cleanup_failed', error='native_unsubscribe_' + type(exc).__name__)
            result[code] = dict(item)
        reply = self._base()
        reply.update(subscriptions=result,
                     state='complete' if all(s['state'] in ('unsubscribed', 'not_subscribed')
                                             for s in result.values()) else 'incomplete')
        return reply

    def _status(self):
        now_ms, monotonic_now, effective_now_ms = self._clock()
        quotes = {}
        for code, observation in self.observations.items():
            quotes[code] = self._health(dict(observation['metadata']), now_ms, monotonic_now,
                                         observation['observed_at'], effective_now_ms)
        for code in self.subscriptions:
            if code not in quotes:
                quotes[code] = {'code': code, 'health': 'not_observed', 'fresh': False,
                                'complete_frame': False, 'source_time_utc_ms': None}
        reply = self._base()
        reply.update(subscriptions={c: dict(s) for c, s in self.subscriptions.items()}, quotes=quotes,
                     max_age_ms=self.max_age_ms, capacity=10, received_at_utc_ms=now_ms,
                     callback_delivery=False, connection_health_owner='memory_transport')
        return reply

    def __call__(self, kind, body):
        if kind in ('market_status', 'market_health'):
            if body != {}:
                raise ValueError('market status body must be empty')
            reply = self._status()
        elif kind in ('market_snapshot', 'market_subscribe', 'market_unsubscribe'):
            codes = _codes(body)
            if kind == 'market_snapshot':
                reply = self._snapshot(codes)
            elif kind == 'market_subscribe':
                reply = self._subscribe(codes)
            else:
                reply = self._unsubscribe(codes)
        else:
            raise ValueError('unsupported read-only market operation')
        return kind + '_reply', reply

    def close(self):
        """Discard all validity and attempt bounded cleanup; remain reusable."""
        self.observations.clear()
        self.generation = os.urandom(16).hex()
        self.sequence = 0
        reply = self._unsubscribe(list(self.subscriptions))
        return {'state': reply['state'], 'remaining_subscriptions': len(self.subscriptions),
                'errors': [{'code': code, 'error': item['error']} for code, item in self.subscriptions.items()]}
