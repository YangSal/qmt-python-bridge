"""External-only qualification CLI; this is not a supported production SDK."""
import argparse
import time
import uuid
from pathlib import Path

from bigqmt_bridge.normalize import normalize_market
from qmt_bridge.protocol import atomic_json, read_result

PROTOCOL = 'qualification-v1'
SAMPLE_DATE = '20260908'


def call(root, operation, args=None, timeout=10):
    root = Path(root)
    rid = uuid.uuid4().hex
    atomic_json(root / 'requests' / (rid + '.json'), {
        'protocol': PROTOCOL, 'request_id': rid, 'operation': operation,
        'args': args or {}, 'deadline': time.time() + timeout})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (root / 'responses' / (rid + '.json')).is_file():
            response = read_result(str(root / 'responses'), rid)
            if response.get('experiment') != PROTOCOL:
                raise ValueError('wrong experiment response')
            return response
        time.sleep(0.1)
    raise TimeoutError('qualification request outcome unknown: %s; do not resubmit downloads' % rid)


def validate_sample(raw, code, period):
    if not isinstance(raw, dict) or raw.get('__qmt_raw_market__') != 1:
        raise ValueError('a raw history read is required; a download acknowledgement is not data')
    frame = normalize_market(raw, [code])[code]
    import pandas as pd
    times = pd.to_datetime(frame['time'], unit='ms', utc=True).dt.tz_convert('Asia/Shanghai')
    if not (times.dt.strftime('%Y%m%d') == SAMPLE_DATE).all():
        raise ValueError('date mismatch')
    if frame['time'].duplicated().any():
        raise ValueError('duplicate timestamps')
    if period != 'tick':
        required = ['open', 'high', 'low', 'close', 'volume', 'amount']
        if not set(required).issubset(frame.columns):
            raise ValueError('missing OHLCV fields')
        if ((frame['high'] < frame[['open', 'low', 'close']].max(axis=1)) |
                (frame['low'] > frame[['open', 'high', 'close']].min(axis=1)) |
                (frame[['volume', 'amount']] < 0).any(axis=1)).any():
            raise ValueError('invalid OHLCV values')
        allowed_rows = {'1d': (1,), '1m': (240, 241), '5m': (48, 49)}[period]
        if len(frame) not in allowed_rows:
            raise ValueError('sample incomplete or different bar convention: %d rows' % len(frame))
        if period != '1d' and times.dt.strftime('%H:%M:%S').iloc[-1] != '15:00:00':
            raise ValueError('last bar is not market close')
        if period in ('1m', '5m'):
            step = 1 if period == '1m' else 5
            expected = set(pd.date_range('2000-01-01 09:%02d' % (30 + step),
                                        '2000-01-01 11:30', freq='%dmin' % step).strftime('%H:%M:%S'))
            expected.update(pd.date_range('2000-01-01 13:%02d' % step,
                                          '2000-01-01 15:00', freq='%dmin' % step).strftime('%H:%M:%S'))
            actual = set(times.dt.strftime('%H:%M:%S'))
            if actual not in (expected, expected | {'09:30:00'}):
                raise ValueError('minute grid missing or different bar convention')
    return {'rows': len(frame), 'columns': list(frame.columns),
            'first_beijing': times.iloc[0].isoformat(), 'last_beijing': times.iloc[-1].isoformat(),
            'historical_tick_completeness_verified': False if period == 'tick' else None}


def sample(root, code, period, download=False):
    args = {'stock_code': code, 'period': period, 'date': SAMPLE_DATE}
    report = {'stock_code': code, 'period': period, 'date': SAMPLE_DATE,
              'download_requested': download, 'ok': False}
    before = call(root, 'read_history', args)
    report['before_raw'] = before
    initial_rows = before['data'].get('data', {}).get(code)
    report['initial_cache_empty'] = (
        initial_rows is None or initial_rows == [] or initial_rows == {} or
        (isinstance(initial_rows, dict) and all(isinstance(v, list) and not v
                                               for v in initial_rows.values())))
    try:
        report['before'] = validate_sample(before['data'], code, period)
        report['initial_cache_valid'] = True
    except Exception as exc:
        report['before_error'] = '%s: %s' % (type(exc).__name__, exc)
        report['initial_cache_valid'] = False
    if not download:
        report['ok'] = report['initial_cache_valid']
        return report
    # Once only. A timeout means unknown, not permission to send a second download.
    try:
        report['download'] = call(root, 'download_history', args, timeout=30)
    except Exception as exc:
        report['download_error'] = '%s: %s' % (type(exc).__name__, exc)
        return report
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            after = call(root, 'read_history', args, timeout=min(10, max(1, deadline - time.monotonic())))
            report['after_raw'] = after
            report['after'] = validate_sample(after['data'], code, period)
            report['ok'] = True
            report['cold_cache_to_ready'] = report['initial_cache_empty']
            break
        except Exception as exc:
            report['after_error'] = '%s: %s' % (type(exc).__name__, exc)
            time.sleep(1)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['capabilities', 'modules', 'full_tick', 'sample'])
    parser.add_argument('--root', default=r'D:\bigqmt-qualification-runtime')
    parser.add_argument('--code', choices=['000001.SZ', '510300.SH', '000300.SH'], default='000001.SZ')
    parser.add_argument('--period', choices=['1d', '1m', '5m', 'tick'], default='1d')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'sample':
            report = sample(args.root, args.code, args.period, args.download)
        else:
            if args.download:
                raise ValueError('--download only belongs to sample')
            report = {'ok': True, 'response': call(args.root, args.command, {'stock_code': args.code})}
    except Exception as exc:
        report = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
    atomic_json(args.output, report)
    print({'ok': report['ok'], 'output': args.output,
           'error': report.get('error') or report.get('download_error') or report.get('after_error')})
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
