"""User-owned disk archive of quotes received through the memory transport."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from qmt_bridge.protocol import atomic_json
from .memory_session import Connection, load_config


def _market_envelope(body, generation=None):
    if (not isinstance(body, dict) or body.get('schema') != 'qmt-market-memory-v1'
            or body.get('mode') != 'poll_snapshot' or body.get('incremental_complete') is not False
            or not isinstance(body.get('generation'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', body['generation'])
            or (generation is not None and body['generation'] != generation)):
        raise ValueError('invalid market envelope or changed generation')
    return body['generation']


def _quote_health(quote, source_high, now_ms, effective_now_ms, max_age_ms):
    health = quote.get('health')
    if health not in ('fresh', 'stale', 'incomplete', 'unavailable', 'out_of_order',
                      'zero_price', 'time_unknown', 'clock_skew'):
        raise ValueError('invalid producer quote health')
    if type(quote.get('complete_frame')) is not bool or type(quote.get('fresh')) is not bool:
        raise ValueError('invalid producer quote validity')
    source = quote.get('source_time_utc_ms')
    if source is not None and (type(source) is not int or not 946684800000 <= source < 4102444800000):
        raise ValueError('invalid producer source timestamp')
    if quote.get('error'):
        return 'unavailable'
    if not quote['complete_frame']:
        return 'incomplete'
    if not isinstance(quote.get('data'), dict):
        raise ValueError('complete quote requires a data object')
    if source is None:
        return 'time_unknown'
    if quote.get('out_of_order') or (source_high is not None and source < source_high):
        return 'out_of_order'
    if now_ms < source:
        return 'clock_skew'
    ages = (quote.get('source_age_ms'), quote.get('effective_source_age_ms'))
    if any(type(age) not in (int, float) or not math.isfinite(age) for age in ages):
        raise ValueError('invalid producer quote age')
    if min(ages) < 0:
        return 'clock_skew'
    if max(ages) > max_age_ms or effective_now_ms - source > max_age_ms:
        return 'stale'
    if health != 'fresh':
        return health
    return 'fresh' if quote['fresh'] else 'unavailable'


def collect(config_path, codes, output_dir, duration=60, interval=.5, timeout=5,
            max_bytes=64*1024*1024):
    if (not isinstance(codes, list) or not 1 <= len(codes) <= 10 or len(set(codes)) != len(codes)
            or any(not isinstance(c, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', c) for c in codes)):
        raise ValueError('provide 1..10 distinct Shanghai/Shenzhen/Beijing codes')
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (duration, interval, timeout))
            or not 0 < duration <= 3600 or not 0 < interval <= 60 or not 0 < timeout <= 10
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1024*1024*1024):
        raise ValueError('invalid bounded collection limits')
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    if config.get('mode') != 'market':
        raise ValueError('an accepted and prepared market memory session is required')
    root = Path(output_dir).resolve()
    private = config_path.parent
    if root == private or root.is_relative_to(private) or private.is_relative_to(root):
        raise ValueError('consumer archive must be separate from private session files')
    root.mkdir(parents=True, exist_ok=True)
    archive_name = 'quotes-' + os.urandom(8).hex() + '.jsonl.gz'
    archive_path = root / archive_name
    report_path = root / (archive_name + '.report.json')
    report = dict(schema='qmt-realtime-consumer-v1', state='incomplete', mode='poll_snapshot',
        incremental_complete=False, realtime_verified=False, bridge_health='connecting',
        snapshots=0, quotes=0, fresh_quotes=0, changed_quotes=0, bytes_uncompressed=0,
        incomplete_snapshots=0, fresh_changed_quotes=0,
        codes=codes, error=None, archive=None, unsubscribe='incomplete',
        started_at_utc_ms=round(time.time()*1000))
    stats = {code: dict(received=0, complete_frames=0, fresh=0, changed=0, fresh_changed=0,
                       last_health='not_observed', first_source_time=None, last_source_time=None)
             for code in codes}
    previous = {}
    last_sequence = None
    generation = None
    connection = None
    try:
        deadline = time.monotonic() + duration + 2*timeout
        connection = Connection(config, timeout, deadline).open()
        subscribed = connection.call('market_subscribe', {'codes': codes})
        generation = _market_envelope(subscribed)
        subscriptions = subscribed.get('subscriptions', {})
        if (subscribed.get('state') != 'complete' or not isinstance(subscriptions, dict)
                or any(subscriptions.get(code, {}).get('state') != 'active' for code in codes)):
            raise ValueError('one or more native subscriptions are not active')
        status = connection.call('market_status')
        _market_envelope(status, generation)
        max_age_ms = status.get('max_age_ms')
        if type(max_age_ms) not in (int, float) or not math.isfinite(max_age_ms) or not 0 < max_age_ms <= 86400000:
            raise ValueError('invalid producer quote-age limit')
        report['max_age_ms'] = max_age_ms
        report['bridge_health'] = 'connected'
        end = time.monotonic() + duration
        clock_start, utc_start = time.monotonic(), round(time.time()*1000)
        with gzip.open(archive_path, 'xb') as stream:
            while time.monotonic() < end:
                started = time.monotonic()
                body = connection.call('market_snapshot', {'codes': codes})
                _market_envelope(body, generation)
                if (not isinstance(body.get('quotes'), dict) or set(body['quotes']) != set(codes)
                        or body.get('state') not in ('complete', 'incomplete')):
                    raise ValueError('invalid market snapshot envelope')
                received_ms = round(time.time()*1000)
                effective_ms = max(received_ms, utc_start + round((time.monotonic()-clock_start)*1000))
                health = {}
                for code in codes:
                    quote = body['quotes'][code]
                    if not isinstance(quote, dict) or quote.get('code') != code:
                        raise ValueError('quote security identity mismatch')
                    sequence = quote.get('bridge_sequence')
                    if (type(sequence) is not int or not 1 <= sequence <= 2**53
                            or (last_sequence is not None and sequence != last_sequence+1)):
                        raise ValueError('bridge quote sequence duplicate, gap or reversal')
                    last_sequence = sequence
                    health[code] = _quote_health(quote, previous.get(code), received_ms, effective_ms, max_age_ms)
                complete = all(q['complete_frame'] for q in body['quotes'].values())
                if (body['state'] == 'complete') != complete:
                    raise ValueError('market snapshot completeness mismatch')
                record = dict(body, consumer_received_at_utc_ms=received_ms, consumer_health=health)
                payload = (json.dumps(record, ensure_ascii=True, allow_nan=False,
                                      separators=(',', ':')) + '\n').encode('utf-8')
                if report['bytes_uncompressed'] + len(payload) > max_bytes:
                    raise BufferError('consumer archive byte budget exceeded')
                # This is the user-authorized archive sink, never bridge IPC.
                stream.write(payload)
                stream.flush()
                report['bytes_uncompressed'] += len(payload)
                report['snapshots'] += 1
                report['incomplete_snapshots'] += int(not complete)
                for code in codes:
                    quote = body['quotes'][code]
                    stats[code]['last_health'] = health[code]
                    if 'data' not in quote:
                        continue
                    old = previous.get(code)
                    source_time = quote.get('source_time_utc_ms')
                    stats[code]['received'] += 1
                    stats[code]['complete_frames'] += int(quote['complete_frame'])
                    report['quotes'] += 1
                    fresh = health[code] == 'fresh'
                    if fresh:
                        stats[code]['fresh'] += 1
                        report['fresh_quotes'] += 1
                    if quote['complete_frame'] and old is not None and source_time is not None and source_time > old:
                        stats[code]['changed'] += 1
                        report['changed_quotes'] += 1
                        if fresh:
                            stats[code]['fresh_changed'] += 1
                            report['fresh_changed_quotes'] += 1
                    if quote['complete_frame'] and source_time is not None and source_time <= received_ms:
                        previous[code] = source_time if old is None else max(old, source_time)
                    if stats[code]['first_source_time'] is None:
                        stats[code]['first_source_time'] = source_time
                    stats[code]['last_source_time'] = source_time
                report['last_receive_utc_ms'] = round(time.time()*1000)
                atomic_json(report_path, dict(report, per_code=stats))
                delay = min(end-time.monotonic(), interval-(time.monotonic()-started))
                if delay > 0:
                    time.sleep(delay)
        report['state'] = ('complete' if not report['incomplete_snapshots'] and
                           all(s['complete_frames'] for s in stats.values()) else 'incomplete')
        report['realtime_verified'] = (report['state'] == 'complete' and
            all(s['fresh_changed'] > 0 and s['last_health'] == 'fresh' for s in stats.values()))
    except Exception as exc:
        report.update(state='incomplete', realtime_verified=False, error=type(exc).__name__,
                      bridge_health='disconnected')
    finally:
        if connection is not None:
            try:
                result = connection.call('market_unsubscribe', {'codes': codes})
                _market_envelope(result, generation)
                left = result.get('subscriptions', {})
                report['unsubscribe'] = 'complete' if result.get('state') == 'complete' and all(
                    left.get(code, {}).get('state') in ('unsubscribed', 'not_subscribed')
                    for code in codes) else 'incomplete'
            except Exception:
                report['unsubscribe'] = 'incomplete'
            finally:
                try:
                    connection.close()
                except Exception:
                    report['close_pending'] = True
                    report['state'] = 'incomplete'
                    report['realtime_verified'] = False
            if report['unsubscribe'] != 'complete':
                report['state'] = 'incomplete'
                report['realtime_verified'] = False
        if archive_path.exists():
            digest = hashlib.sha256()
            with archive_path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(chunk)
            report['archive'] = dict(file=archive_name, bytes=archive_path.stat().st_size,
                                     sha256=digest.hexdigest())
        report['per_code'] = stats
        report['finished_at_utc_ms'] = round(time.time()*1000)
        report['bridge_health_at_finish'] = report['bridge_health']
        report['bridge_health'] = ('closing' if report.get('close_pending') else
                                   'closed' if report['error'] is None else 'disconnected')
        atomic_json(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--codes', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--interval', type=float, default=.5)
    args = parser.parse_args(argv)
    result = collect(args.config, args.codes.split(','), args.output_dir, args.seconds, args.interval)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['state'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
