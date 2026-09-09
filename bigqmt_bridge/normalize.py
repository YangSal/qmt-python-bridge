"""Normalize explicit QMT shapes without inventing missing fields or dates."""
import json
import math
from functools import lru_cache
from importlib.resources import files
from numbers import Real

import pandas as pd

from . import QmtDataError

DIVID_FIELDS = ['interest', 'stockBonus', 'stockGift', 'allotNum', 'allotPrice', 'gugai', 'dr']

def as_frame(value):
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, dict) and value.get('__frame__') is True:
        return pd.DataFrame(value['data'], columns=value['columns'], index=value['index'])
    try:
        return pd.DataFrame(value)
    except Exception as exc:
        raise QmtDataError('unsupported frame shape') from exc


def beijing_date(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value)
    if text.isdigit() and len(text) == 8:
        return pd.to_datetime(text, format='%Y%m%d', errors='raise').normalize()
    if text.isdigit() and len(text) in (12, 13):
        return (pd.to_datetime(int(text), unit='ms', utc=True)
                .tz_convert('Asia/Shanghai').tz_localize(None).normalize())
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise QmtDataError('invalid date')
    if result.tzinfo:
        result = result.tz_convert('Asia/Shanghai').tz_localize(None)
    return result.normalize()


def utc_ms(values):
    series = pd.Series(values).reset_index(drop=True)
    if series.isna().any():
        raise QmtDataError('missing market timestamp')
    text = series.astype(str)
    if text.str.fullmatch(r'\d{12,13}').all():
        return pd.to_numeric(series, errors='raise').astype('int64')
    if text.str.fullmatch(r'\d{14}').all():
        dates = pd.to_datetime(text, format='%Y%m%d%H%M%S', errors='raise')
    elif text.str.fullmatch(r'\d{8}').all():
        dates = pd.to_datetime(text, format='%Y%m%d', errors='raise')
    else:
        if pd.api.types.is_numeric_dtype(series):
            raise QmtDataError('time is not UTC milliseconds or a QMT calendar timestamp')
        dates = pd.to_datetime(series, errors='raise')
    if dates.dt.tz is None:
        dates = dates.dt.tz_localize('Asia/Shanghai')
    return (dates.dt.tz_convert('UTC').astype('int64') // 1_000_000).astype('int64')


def normalize_market(raw, codes):
    if not isinstance(raw, dict):
        raise QmtDataError('market result must be keyed by stock code')
    if raw.get('__qmt_raw_market__') == 1:
        fields, data = raw['fields'], raw['data']
        if not isinstance(fields, list) or not isinstance(data, dict):
            raise QmtDataError('invalid raw market envelope')
        # Assemble outside QMT, preserving UTC time even when fields omitted it.
        # The terminal's stime may use the Windows timezone, not Beijing time.
        columns = (fields if 'stime' in fields else ['stime'] + fields) if fields else None
        raw = {}
        for code, rows in data.items():
            if columns:
                if isinstance(rows, dict):
                    complete = set(columns).issubset(rows)
                elif isinstance(rows, list):
                    complete = all(set(columns).issubset(row) if isinstance(row, dict)
                                   else isinstance(row, list) and len(row) == len(columns)
                                   for row in rows)
                else:
                    complete = False
                if not complete:
                    raise QmtDataError('raw market fields/row width incomplete: ' + code)
            selected = columns
            has_time = (isinstance(rows, dict) and 'time' in rows) or (
                isinstance(rows, list) and any(isinstance(row, dict) and 'time' in row for row in rows))
            if columns and has_time and 'time' not in columns:
                selected = ['time'] + columns
            raw[code] = pd.DataFrame(rows, columns=selected)
    result = {}
    for code in codes:
        if code not in raw:
            raise QmtDataError('missing market result: ' + code)
        original = as_frame(raw[code])
        frame = original.reset_index(drop=True)
        if frame.empty:
            raise QmtDataError('empty QMT cache: %s; download/verify in QMT before retrying' % code)
        source = next((name for name in ('time', 'stime', 'datetime') if name in frame), None)
        try:
            frame['time'] = utc_ms(frame[source] if source else original.index).values
        except Exception as exc:
            raise QmtDataError('invalid market time: ' + code) from exc
        validate_market_values(frame)
        result[code] = frame.drop(columns=['index', 'stime', 'datetime'], errors='ignore').sort_values('time').reset_index(drop=True)
    return result


def validate_market_values(frame):
    """Validate present core market fields without rewriting values or financial data."""
    for field in ('time', 'open', 'high', 'low', 'close', 'lastPrice', 'volume', 'amount'):
        if field not in frame:
            continue  # The caller's requested-field contract handles missing columns.
        valid = frame[field].map(lambda value: isinstance(value, Real)
                                and not isinstance(value, bool) and math.isfinite(value))
        if not valid.all():
            raise QmtDataError('invalid/non-finite core market value: ' + field)


def normalize_divid(raw, start='', end=''):
    if isinstance(raw, dict) and raw.get('__frame__') is not True:
        if not raw:
            return pd.DataFrame(columns=DIVID_FIELDS)
        frame = pd.DataFrame.from_dict(raw, orient='index')
        if all(isinstance(col, int) for col in frame.columns):
            if len(frame.columns) != 7:
                raise QmtDataError('dividend event must have seven fields')
            frame.columns = DIVID_FIELDS
    else:
        frame = as_frame(raw)
    if frame.empty:
        return frame
    if not set(DIVID_FIELDS).issubset(frame.columns):
        raise QmtDataError('dividend fields missing')
    frame.index = pd.DatetimeIndex([beijing_date(v) for v in frame.index], name='date')
    if start:
        frame = frame.loc[frame.index >= beijing_date(start[:8])]
    if end:
        frame = frame.loc[frame.index <= beijing_date(end[:8])]
    return frame.sort_index()


@lru_cache(maxsize=1)
def financial_schema():
    # A bundled interface contract, not a guarantee of broker field availability.
    resource = files('bigqmt_bridge').joinpath('schemas/financial.json')
    return json.loads(resource.read_text(encoding='utf-8'))


def normalize_financial(raw, code, table, prefix, fields):
    data = raw.get(code) if isinstance(raw, dict) else None
    if data is None:
        raise QmtDataError('financial code missing: ' + code)
    if isinstance(data, dict) and data.get('__frame__') is not True:
        selected = {name.split('.', 1)[-1]: values for name, values in data.items()
                    if name.startswith(prefix + '.')}
        if not selected:
            raise QmtDataError('financial table missing: ' + table)
        frame = as_frame(selected)
    else:
        frame = as_frame(data)
        frame.columns = [c.split('.', 1)[-1] for c in frame.columns]
    missing = set(fields) - set(frame.columns)
    if missing or frame.empty:
        raise QmtDataError('financial schema/cache incomplete %s: %s' % (table, sorted(missing)))
    for col in ('m_timetag', 'm_anntime', 'endDate', 'declareDate'):
        if col in frame:
            try:
                frame[col] = frame[col].map(lambda v: beijing_date(v).strftime('%Y%m%d'))
            except Exception as exc:
                raise QmtDataError('invalid financial identity date: ' + col) from exc
    identity = ['m_timetag', 'm_anntime']
    if table == 'Capital':
        identity = ['m_timetag']
    elif table in ('Holdernum', 'Top10holder', 'Top10flowholder'):
        identity = ['endDate']
        if table != 'Holdernum':
            identity.append('rank')
            rank = pd.to_numeric(frame['rank'], errors='coerce')
            if rank.isna().any() or not ((rank >= 1) & (rank <= 10) & (rank % 1 == 0)).all():
                raise QmtDataError('invalid financial identity rank')
            frame['rank'] = rank.astype('int64')
    if set(identity) - set(frame.columns) or frame[identity].isna().any().any():
        raise QmtDataError('financial identity missing: ' + table)
    distinct = frame[fields].drop_duplicates()
    if distinct.duplicated(identity, keep=False).any():
        raise QmtDataError('conflicting financial rows share one persisted identity: ' + table)
    return frame[fields].reset_index(drop=True)
