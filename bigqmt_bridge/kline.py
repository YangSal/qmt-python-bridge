"""Strict original-UTC K-line validation for automatic history jobs."""
import math
from numbers import Integral, Real

import pandas as pd

from . import QmtDataError

FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount')


def original_frames(raw, codes):
    """Build frames without synthesizing timestamps from stime or an index."""
    if not isinstance(raw, dict):
        raise QmtDataError('market result must be keyed by stock code')
    fields = None
    if raw.get('__qmt_raw_market__') == 1:
        fields, raw = raw.get('fields'), raw.get('data')
        if not isinstance(fields, list) or not isinstance(raw, dict):
            raise QmtDataError('invalid raw market envelope')
        fields = (fields if 'stime' in fields else ['stime'] + fields) if fields else None
    result = {}
    for code in codes:
        try:
            value = raw[code]
            if isinstance(value, pd.DataFrame):
                frame = value.copy()
            elif isinstance(value, dict) and value.get('__frame__') is True:
                frame = pd.DataFrame(value['data'], columns=value['columns'], dtype=object)
            else:
                # Dict rows must retain extra original time even if fields omitted it.
                columns = fields if isinstance(value, list) and value and isinstance(value[0], list) else None
                frame = pd.DataFrame(value, columns=columns, dtype=object)
            if frame.empty or 'time' not in frame or not frame.columns.is_unique:
                raise ValueError('missing original time or empty cache')
            if not frame['time'].map(lambda v: isinstance(v, Integral) and not isinstance(v, bool)).all():
                raise ValueError('original time must be numeric integer UTC milliseconds')
            if frame['time'].duplicated().any():
                raise ValueError('duplicate original UTC time')
            # Epoch values must be convertible without overflow.
            pd.to_datetime(frame['time'].tolist(), unit='ms', utc=True)
            result[code] = frame.reset_index(drop=True)
        except Exception as exc:
            raise QmtDataError('invalid original UTC market time/shape %s: %s' % (code, exc)) from exc
    return result


def validate_kline(raw, stock_code, period, date):
    """Return compact structural evidence, or raise for incomplete/invalid bars."""
    if period not in ('1d', '1m', '5m'):
        raise QmtDataError('unsupported K-line period')
    frame = original_frames(raw, [stock_code])[stock_code]
    missing = set(FIELDS) - set(frame.columns)
    if missing:
        raise QmtDataError('K-line fields missing: ' + ','.join(sorted(missing)))
    for field in FIELDS:
        if not frame[field].map(lambda v: isinstance(v, Real) and not isinstance(v, bool)
                               and math.isfinite(v)).all():
            raise QmtDataError('invalid/non-finite K-line value: ' + field)
    if (frame[['volume', 'amount']] < 0).any().any():
        raise QmtDataError('negative K-line volume/amount')
    if (frame[['open', 'high', 'low', 'close']] < 0).any().any():
        raise QmtDataError('negative K-line price')
    if ((frame['low'] > frame[['open', 'close', 'high']].min(axis=1)).any() or
            (frame['high'] < frame[['open', 'close', 'low']].max(axis=1)).any()):
        raise QmtDataError('impossible K-line OHLC relationships')
    times = pd.to_datetime(frame['time'].tolist(), unit='ms', utc=True).tz_convert('Asia/Shanghai').sort_values()
    if not (times.strftime('%Y%m%d') == date).all():
        raise QmtDataError('K-line date differs from requested Beijing day')
    if period == '1d':
        if len(times) != 1:
            raise QmtDataError('daily K-line requires exactly one row')
    else:
        step = 1 if period == '1m' else 5
        expected = set(range(9 * 60 + 30 + step, 11 * 60 + 31, step))
        expected.update(range(13 * 60 + step, 15 * 60 + 1, step))
        actual = set(times.hour * 60 + times.minute)
        if (any(times.second) or any(times.microsecond) or
                len(actual) != len(times) or
                actual not in (expected, expected | {9 * 60 + 30})):
            raise QmtDataError('incomplete/nonstandard %s K-line grid' % period)
    return {'rows': len(frame), 'first_beijing': times[0].isoformat(),
            'last_beijing': times[-1].isoformat()}
