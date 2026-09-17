"""Frozen, resumable full-market historical archives in external Python."""
import argparse
import datetime as dt
import hashlib
import json
import math
import re
import time
from pathlib import Path

from qmt_bridge.auto_protocol import FileLock
from qmt_bridge.protocol import atomic_json, load_json
from .archive import verify_bars
from .auto_backend import AutomaticBackend
from .backend import create_backend
from .collector import _scope, collect_history
from .config import load_config


FACTOR_FIELDS = ('interest', 'stockBonus', 'stockGift', 'allotNum', 'allotPrice', 'gugai', 'dr')
MAX_JSON = 32 * 1024 * 1024


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be finite and positive')


def _origin(source):
    if not isinstance(source, AutomaticBackend):
        raise ValueError('market collector requires AutomaticBackend')
    return {'bridge_dir': str(source.transport.root), 'job_dir': str(source.downloads.root)}


def _root(source, output_dir):
    root = Path(output_dir).resolve()
    for runtime in _origin(source).values():
        path = Path(runtime).resolve()
        if root.is_relative_to(path) or path.is_relative_to(root):
            raise ValueError('output directory must be separate from bridge IPC/job directories')
    return root


def _codes(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 50000 or
            any(not isinstance(c, str) or not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', c) for c in values)):
        raise ValueError('codes must be a nonempty list of six-digit SH/SZ/BJ codes')
    result = sorted(set(values))
    if len(result) > 20000:
        raise ValueError('universe exceeds 20000 codes')
    return result


def discover(source, output, max_seconds=30):
    """Persist the real sector names without inferring identifiers or membership."""
    _positive(max_seconds, 'max_seconds')
    path = Path(output).resolve()
    _root(source, path)
    if path.exists():
        raise ValueError('discovery output exists; choose a fresh snapshot path')
    original = source.transport.timeout
    try:
        source.transport.timeout = min(original, max_seconds, 30)
        result = {'schema': 'qmt-sector-discovery-v1', 'created_at_utc': _now(),
                  'source': _origin(source), 'sectors': source.get_sector_list()}
        atomic_json(path, result)
        return result
    finally:
        source.transport.timeout = original


def create_plan(source, output_dir, dates, periods, *, sectors=None, codes=None,
                universe=None, provenance=None, shard_size=10, max_seconds=300,
                allow_source_runtime_change=False):
    """Freeze exactly one selected universe. Planning never downloads history."""
    root = _root(source, output_dir)
    _scope(['000001.SZ'], periods, dates)
    _positive(max_seconds, 'max_seconds')
    if type(shard_size) is not int or not 1 <= shard_size <= 10:
        raise ValueError('shard_size must be an integer in 1..10')
    if sum(v is not None for v in (sectors, codes, universe)) != 1:
        raise ValueError('select exact QMT sectors, a frozen snapshot, or an explicit code list')
    if universe is not None:
        if (not isinstance(universe, dict) or universe.get('schema') != 'qmt-sector-universe-v1' or
                not isinstance(universe.get('source'), dict) or
                not isinstance(universe['source'].get('bridge_dir'), str) or
                not isinstance(universe.get('captured_at_utc'), str) or
                not isinstance(universe.get('sectors'), dict) or not universe['sectors']):
            raise ValueError('invalid frozen QMT universe snapshot')
        if (universe['source']['bridge_dir'] != str(source.transport.root) and
                allow_source_runtime_change is not True):
            raise ValueError('snapshot source runtime differs; explicit allow_source_runtime_change required')
        stamp = dt.datetime.fromisoformat(universe['captured_at_utc'].replace('Z', '+00:00'))
        if stamp.utcoffset() != dt.timedelta(0):
            raise ValueError('snapshot capture time must have explicit UTC timezone')
        sectors = list(universe['sectors'])
    if sectors is not None and (not isinstance(sectors, list) or not 1 <= len(sectors) <= 100 or
            any(not isinstance(s, str) or not s.strip() for s in sectors) or len(set(sectors)) != len(sectors)):
        raise ValueError('sectors must be 1..100 unique exact QMT sector names')
    if codes is not None:
        _codes(codes)
        if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 2000:
            raise ValueError('explicit code lists require a bounded nonempty provenance description')
    with FileLock(root / 'market.lock'):
        if (root / 'plan.json').exists() or (root / 'shards').exists():
            raise ValueError('plan is frozen or output already exists; use run to resume')
        sources = []
        original = source.transport.timeout
        deadline = time.monotonic() + max_seconds
        try:
            if universe is not None:
                for name, members in universe['sectors'].items():
                    _codes(members)
                    sources.append({'kind': 'qmt_sector', 'name': name, 'members': members})
            elif sectors is not None:
                source.transport.timeout = min(original, max_seconds, 30)
                available = source.get_sector_list()
                if any(s not in available for s in sectors):
                    raise ValueError('selected sector name is absent from the QMT listing')
                for name in sectors:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('universe discovery budget exhausted; no plan frozen')
                    source.transport.timeout = min(original, remaining, 30)
                    members = source.get_stock_list_in_sector(name)
                    _codes(members)
                    sources.append({'kind': 'qmt_sector', 'name': name, 'members': members})
            else:
                sources.append({'kind': 'explicit_codes', 'provenance': provenance, 'members': codes})
        finally:
            source.transport.timeout = original
        universe_codes = _codes([c for entry in sources for c in entry['members']])
        plan = {'schema': 'market-history-plan-v1', 'created_at_utc': _now(),
                'source': _origin(source), 'universe_sources': sources, 'codes': universe_codes,
                'dates': list(dates), 'periods': sorted(periods), 'factor_cutoff': dates[-1],
                'shard_size': shard_size, 'shards': [
                    {'id': '%05d' % (offset // shard_size), 'codes': universe_codes[offset:offset + shard_size]}
                    for offset in range(0, len(universe_codes), shard_size)],
                'total_units': len(universe_codes) * (len(periods) * len(dates) + 1)}
        if universe is not None:
            plan['snapshot'] = universe
            plan['source_runtime_change_authorized'] = allow_source_runtime_change is True
        plan['plan_sha256'] = _hash(plan)
        atomic_json(root / 'plan.json', plan)
        return plan


def _read_plan(root, source):
    plan = load_json(root / 'plan.json', MAX_JSON)
    if not isinstance(plan, dict):
        raise ValueError('invalid plan')
    content = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    if plan.get('schema') != 'market-history-plan-v1' or _hash(content) != plan.get('plan_sha256'):
        raise ValueError('plan digest mismatch')
    if plan.get('source') != _origin(source):
        raise ValueError('plan source mismatch')
    # Validate paths and dimensions independently, even if someone recomputed the digest.
    codes = _codes(plan['codes'])
    _scope([codes[0]], plan['periods'], plan['dates'])
    size = plan['shard_size']
    if type(size) is not int or not 1 <= size <= 10:
        raise ValueError('invalid plan shard size')
    shards = [{'id': '%05d' % (o // size), 'codes': codes[o:o + size]} for o in range(0, len(codes), size)]
    if (codes != plan['codes'] or shards != plan['shards'] or
            plan['factor_cutoff'] != plan['dates'][-1] or
            plan['total_units'] != len(codes) * (len(plan['dates']) * len(plan['periods']) + 1)):
        raise ValueError('invalid plan dimensions')
    return plan


def _identities(plan, shard):
    items = []
    for code in shard['codes']:
        for date in plan['dates']:
            for period in plan['periods']:
                items.append({'kind': 'history', 'code': code, 'period': period, 'date': date})
        items.append({'kind': 'factors', 'code': code, 'cutoff': plan['factor_cutoff'],
            'request_id': _hash({'plan': plan['plan_sha256'], 'code': code, 'operation': 'divid_factors'})[:32]})
    return items


def _read_shard(root, plan, shard):
    path = root / 'shards' / shard['id'] / 'progress.json'
    identities = _identities(plan, shard)
    if not path.exists():
        return {'schema': 'market-history-shard-v1', 'plan_sha256': plan['plan_sha256'],
                'shard_id': shard['id'], 'items': [dict(i, state='pending', error=None,
                    reason=None, file=None) for i in identities]}
    report = load_json(path, 2 * 1024 * 1024)
    if (not isinstance(report, dict) or report.get('schema') != 'market-history-shard-v1' or
            report.get('plan_sha256') != plan['plan_sha256'] or report.get('shard_id') != shard['id'] or
            not isinstance(report.get('items'), list) or len(report['items']) != len(identities)):
        raise ValueError('invalid shard progress identity')
    for item, identity in zip(report['items'], identities):
        if (not isinstance(item, dict) or any(item.get(k) != v for k, v in identity.items()) or
                item.get('state') not in ('pending', 'running', 'saved', 'failed') or 'file' not in item):
            raise ValueError('invalid shard unit identity/state')
    return report


def _event_date(value):
    if type(value) not in (str, int):
        raise ValueError('event date must be YYYYMMDD, ISO date, or UTC epoch milliseconds')
    text = str(value)
    if re.fullmatch(r'[0-9]{8}', text):
        date = dt.datetime.strptime(text, '%Y%m%d').date()
    elif re.fullmatch(r'[0-9]{12,13}', text):
        date = dt.datetime.fromtimestamp(int(text) / 1000, dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))).date()
    elif re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ].+)?', text):
        stamp = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(dt.timezone(dt.timedelta(hours=8)))
        date = stamp.date()
    else:
        raise ValueError('unsupported event date')
    if date.year < 1900:
        raise ValueError('event date predates supported securities history')
    return date.strftime('%Y%m%d')


def validate_factors(raw, cutoff):
    """Validate every returned event before retaining all events through cutoff."""
    if not isinstance(raw, dict):
        raise ValueError('factor response must be an event mapping or frame envelope')
    if raw.get('__frame__') is True:
        columns, indices, values = raw.get('columns'), raw.get('index'), raw.get('data')
        if (not isinstance(columns, list) or len(columns) != 7 or set(columns) != set(FACTOR_FIELDS) or
                not isinstance(indices, list) or not isinstance(values, list) or len(indices) != len(values)):
            raise ValueError('invalid factor frame shape')
        pairs = list(zip(indices, values))
    else:
        columns = list(FACTOR_FIELDS)
        pairs = list(raw.items())
    events, seen = [], set()
    for date_value, values in pairs:
        date = _event_date(date_value)
        if date in seen:
            raise ValueError('duplicate factor event date')
        seen.add(date)
        if isinstance(values, dict):
            if set(values) != set(FACTOR_FIELDS):
                raise ValueError('factor event must have exactly seven named fields')
            event = dict(values)
        elif isinstance(values, list) and len(values) == 7:
            event = dict(zip(columns, values))
        else:
            raise ValueError('factor event must contain seven values')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in event.values()):
            raise ValueError('factor fields must be finite numeric values, excluding booleans')
        if date <= cutoff:
            events.append(dict(date=date, **event))
    return {'events': sorted(events, key=lambda e: e['date']), 'returned_events': len(pairs),
            'excluded_after_cutoff': len(pairs) - len(events),
            'state': 'events' if events else 'no_events',
            'evidence': ('qmt_returned_empty_event_mapping' if not pairs else
                         'qmt_returned_events_filtered_through_cutoff')}


def _factor_path(shard_root, item):
    return shard_root / 'factors' / (item['code'] + '.json')


def _cell_root(shard_root, item):
    return shard_root / 'cells' / item['code'] / item['period'] / item['date']


def _verify_factor(path, metadata):
    if (not isinstance(metadata, dict) or type(metadata.get('bytes')) is not int or
            not 0 < metadata['bytes'] <= MAX_JSON or not isinstance(metadata.get('sha256'), str)):
        raise ValueError('invalid factor archive metadata')
    if path.stat().st_size != metadata['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
        raise ValueError('factor archive checksum mismatch; original preserved')


def _verify_saved(shard_root, item):
    if item['kind'] == 'factors':
        _verify_factor(_factor_path(shard_root, item), item['file'])
    else:
        cell_root = _cell_root(shard_root, item)
        report = load_json(cell_root / 'collection.json', 1024 * 1024)
        expected = {'codes': [item['code']], 'periods': [item['period']], 'dates': [item['date']]}
        if report.get('state') != 'complete' or report.get('scope') != expected or len(report.get('items', [])) != 1:
            raise ValueError('saved history manifest mismatch')
        path = cell_root / 'history' / item['period'] / item['code'] / (item['date'] + '.jsonl.gz')
        verify_bars(path, report['items'][0]['file'])


def _factor_request_id(source, shard_root, item, report, allow_refresh):
    """Reconcile durable read intents before considering an explicitly requested refresh."""
    audit_root = shard_root / 'factor-attempts' / item['code']
    paths = sorted(audit_root.glob('*.json'))
    if len(paths) > 8:
        raise ValueError('factor refresh audit exceeds limit')
    history, current = [], item['request_id']
    for number, path in enumerate(paths, 1):
        audit = load_json(path, 8192)
        expected_id = _hash({'original_request_id': item['request_id'],
                            'attempt': number, 'operation': 'divid_factors'})[:32]
        expected = {'schema': 'market-factor-refresh-v1', 'attempt': number,
                    'original_request_id': item['request_id'], 'previous_request_id': current,
                    'request_id': expected_id, 'operation': 'divid_factors',
                    'args': {'stock_code': item['code']}, 'reason': 'explicit_retry_failed'}
        if (path.name != '%04d.json' % number or not isinstance(audit, dict) or
                set(audit) != set(expected) | {'previous_state', 'created_at_utc'} or
                any(audit.get(key) != value for key, value in expected.items()) or
                audit['previous_state'] not in ('failed', 'expired') or
                not isinstance(audit['created_at_utc'], str)):
            raise ValueError('invalid factor refresh audit identity')
        history.append(audit)
        current = expected_id
    recorded = item.get('refresh_history', [])
    if not isinstance(recorded, list) or recorded != history[:len(recorded)]:
        raise ValueError('factor refresh audit disagrees with shard progress')
    if history:
        item['refresh_history'] = history
        atomic_json(shard_root / 'progress.json', report)
    if allow_refresh:
        previous = source.transport.lookup(current)
        if previous['state'] in ('failed', 'expired'):
            # Confirm the durable request really was this read, not another operation.
            record = source.transport._load_record(current)
            if record['operation'] != 'divid_factors' or record['args'] != {'stock_code': item['code']}:
                raise ValueError('factor request record identity mismatch')
            if len(history) >= 8:
                raise ValueError('factor refresh limit reached; preserve audit for review')
            number = len(history) + 1
            request_id = _hash({'original_request_id': item['request_id'],
                               'attempt': number, 'operation': 'divid_factors'})[:32]
            audit = {'schema': 'market-factor-refresh-v1', 'attempt': number,
                     'original_request_id': item['request_id'], 'previous_request_id': current,
                     'previous_state': previous['state'], 'request_id': request_id,
                     'operation': 'divid_factors', 'args': {'stock_code': item['code']},
                     'reason': 'explicit_retry_failed', 'created_at_utc': _now()}
            # One new immutable intent per explicit retry; never overwrite an older attempt.
            atomic_json(audit_root / ('%04d.json' % number), audit)
            history.append(audit)
            item['refresh_history'] = history
            atomic_json(shard_root / 'progress.json', report)
            current = request_id
    return current


def _factor_unit(source, shard_root, item, report, allow_refresh=False):
    path = _factor_path(shard_root, item)
    if item['file'] is not None and path.exists():
        _verify_factor(path, item['file'])
        return
    deadline = time.monotonic() + source.transport.timeout
    request_id = _factor_request_id(source, shard_root, item, report, allow_refresh)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('factor unit budget exhausted before request publication')
    source.transport.timeout = min(source.transport.timeout, remaining)
    # Existing worker calls ContextInfo.get_divid_factors(code) without a date.
    # Use its read-only operation, without asserting the inherited GUI cache gate.
    request_id = source.transport.submit('divid_factors', {'stock_code': item['code']},
                                         request_id=request_id)
    response = source.transport.wait(request_id)
    if response['state'] != 'returned':
        raise ValueError('factor request %s: %s' % (request_id, response.get('error') or response['state']))
    data = validate_factors(response['data'], item['cutoff'])
    data.update(schema='market-factor-events-v1', code=item['code'], cutoff=item['cutoff'],
                request_id=request_id, fetched_at_utc=_now())
    atomic_json(path, data)
    item['file'] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'events': len(data['events']), 'event_state': data['state']}


def _bucket(state):
    return 'completed' if state == 'saved' else 'failed' if state == 'failed' else 'remaining'


def _counts(plan, reports):
    counts = {'total': plan['total_units'], 'completed': 0, 'failed': 0, 'remaining': 0}
    for report in reports:
        for item in report['items']:
            counts[_bucket(item['state'])] += 1
    return counts


def _transition(item, counts, state, **changes):
    counts[_bucket(item['state'])] -= 1
    item.update(state=state, **changes)
    counts[_bucket(state)] += 1


def _summary(plan, counts, started, attempted, cursor, stop_reason=None, bridge_check=None):
    elapsed = time.monotonic() - started
    cycle_complete = cursor == counts['total']
    return dict(counts, schema='market-history-summary-v1', plan_sha256=plan['plan_sha256'],
                state='complete' if counts['completed'] == counts['total'] and cycle_complete else 'incomplete',
                next_unit=0 if cycle_complete else cursor, verification_cycle_complete=cycle_complete,
                unchecked_units=counts['total'] - cursor,
                stop_reason=stop_reason, bridge_check=bridge_check,
                updated_at_utc=_now(), run_units=attempted, run_seconds=round(elapsed, 3),
                run_units_per_second=round(attempted / elapsed, 5) if elapsed > 0 else None)


def _resume_cursor(root, plan):
    path = root / 'summary.json'
    if not path.exists():
        return 0
    summary = load_json(path, 1024 * 1024)
    if (not isinstance(summary, dict) or summary.get('schema') != 'market-history-summary-v1' or
            summary.get('plan_sha256') != plan['plan_sha256'] or type(summary.get('next_unit')) is not int or
            not 0 <= summary['next_unit'] < plan['total_units']):
        raise ValueError('invalid market progress cursor')
    return summary['next_unit']


def run_plan(source, output_dir, *, max_seconds=300, max_units=100, unit_seconds=30,
             retry_failed=False, on_progress=None):
    """Resume pending units; --retry-failed reconciles the same durable identities."""
    root = _root(source, output_dir)
    for value, name in [(max_seconds, 'max_seconds'), (unit_seconds, 'unit_seconds')]:
        _positive(value, name)
    if type(max_units) is not int or not 1 <= max_units <= 100000:
        raise ValueError('max_units must be in 1..100000')
    if on_progress is not None and not callable(on_progress):
        raise ValueError('on_progress must be callable')
    started, attempted = time.monotonic(), 0
    consecutive_failures, stop_reason, bridge_check = 0, None, None
    deadline = started + max_seconds
    with FileLock(root / 'market.lock'):
        plan = _read_plan(root, source)
        reports = [_read_shard(root, plan, s) for s in plan['shards']]
        counts = _counts(plan, reports)
        units = [(report, item) for report in reports for item in report['items']]
        cursor = _resume_cursor(root, plan)
        if retry_failed:
            cursor = min(cursor, next((position for position, (_, item) in enumerate(units)
                if item['state'] == 'failed'), cursor))
        original = source.transport.timeout
        try:
            for position in range(cursor, len(units)):
                report, item = units[position]
                shard_root = root / 'shards' / report['shard_id']
                if time.monotonic() >= deadline:
                    stop_reason = 'budget_exhausted'
                    break
                if item['state'] == 'saved':
                    try:
                        _verify_saved(shard_root, item)
                        cursor = position + 1
                        continue
                    except FileNotFoundError:
                        _transition(item, counts, 'pending', error='local archive missing; recovery required')
                        atomic_json(shard_root / 'progress.json', report)
                    except Exception as exc:
                        _transition(item, counts, 'failed', reason='archive_corrupt', error=str(exc)[:2000])
                        atomic_json(shard_root / 'progress.json', report)
                        cursor = position + 1
                        continue
                if item['state'] == 'failed' and not retry_failed:
                    cursor = position + 1
                    continue
                if attempted >= max_units:
                    stop_reason = 'unit_limit'
                    break
                budget = min(unit_seconds, deadline - time.monotonic())
                if budget <= 0:
                    stop_reason = 'budget_exhausted'
                    break
                attempted += 1
                allow_factor_refresh = retry_failed and item['state'] == 'failed'
                _transition(item, counts, 'running', error=None, reason=None)
                atomic_json(shard_root / 'progress.json', report)
                try:
                    if item['kind'] == 'history':
                        result = collect_history(source, [item['code']], [item['period']], [item['date']],
                                                 _cell_root(shard_root, item), max_seconds=budget)
                        if result['state'] != 'complete':
                            raise ValueError('; '.join(result.get('errors', [])) or 'history is incomplete')
                    else:
                        source.transport.timeout = min(original, budget)
                        _factor_unit(source, shard_root, item, report, allow_factor_refresh)
                    _transition(item, counts, 'saved', error=None, reason=None)
                    consecutive_failures = 0
                except Exception as exc:
                    _transition(item, counts, 'failed', error=('%s: %s' % (type(exc).__name__, exc))[:2000],
                                reason='history_incomplete' if item['kind'] == 'history' else 'factor_error')
                    consecutive_failures += 1
                finally:
                    source.transport.timeout = original
                cursor = position + 1
                report['updated_at_utc'] = _now()
                atomic_json(shard_root / 'progress.json', report)
                if consecutive_failures >= 3:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        stop_reason = 'budget_exhausted'
                    else:
                        bridge_check = {'checked_at_utc': _now(), 'state': 'available', 'error': None}
                        try:
                            source.transport.timeout = min(original, unit_seconds, remaining, 5.0)
                            health = source.probe()
                            if not isinstance(health, dict) or health.get('automatic_kline') != 'auto-kline-v1':
                                raise ValueError('automatic history worker probe unavailable')
                        except Exception as exc:
                            bridge_check.update(state='unavailable',
                                error=('%s: %s' % (type(exc).__name__, exc))[:2000])
                            stop_reason = 'bridge_unavailable'
                        finally:
                            source.transport.timeout = original
                        consecutive_failures = 0
                summary = _summary(plan, counts, started, attempted, cursor, stop_reason, bridge_check)
                atomic_json(root / 'summary.json', summary)
                if on_progress is not None:
                    on_progress(summary)
                if stop_reason is not None:
                    break
            summary = _summary(plan, counts, started, attempted, cursor, stop_reason, bridge_check)
            atomic_json(root / 'summary.json', summary)
            return summary
        finally:
            source.transport.timeout = original


def _csv(value):
    return [v.strip() for v in value.split(',') if v.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    for command in ('discover', 'plan', 'run'):
        sub = subs.add_parser(command)
        sub.add_argument('--config', required=True)
        sub.add_argument('--max-seconds', type=float, default=30 if command == 'discover' else 300)
        if command == 'discover':
            sub.add_argument('--output', required=True)
        else:
            sub.add_argument('--output-dir', required=True)
        if command == 'plan':
            choice = sub.add_mutually_exclusive_group(required=True)
            choice.add_argument('--sectors-file', help='JSON array of exact names from discovery')
            choice.add_argument('--codes-file', help='JSON object with codes and provenance')
            choice.add_argument('--universe-file', help='frozen qmt-sector-universe-v1 snapshot')
            sub.add_argument('--dates', type=_csv, required=True)
            sub.add_argument('--periods', type=_csv, default=['1d', '1m', '5m'])
            sub.add_argument('--shard-size', type=int, default=10)
            sub.add_argument('--allow-source-runtime-change', action='store_true',
                             help='explicitly bind a snapshot from the same terminal to another runtime')
        if command == 'run':
            sub.add_argument('--max-units', type=int, default=100)
            sub.add_argument('--unit-seconds', type=float, default=30)
            sub.add_argument('--retry-failed', action='store_true')
            sub.add_argument('--progress-every', type=int, default=1, help='print progress after this many attempted units')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if config.get('history_mode') != 'auto' or config.get('backend', 'file_bridge') != 'file_bridge':
            raise ValueError('requires explicit history_mode=auto and file_bridge backend')
        source = create_backend(config)
        if args.command == 'discover':
            result = discover(source, args.output, args.max_seconds)
            result = {'state': 'complete', 'sector_count': len(result['sectors']), 'output': str(Path(args.output).resolve())}
        elif args.command == 'plan':
            if args.universe_file:
                selection = {'universe': load_json(Path(args.universe_file), MAX_JSON)}
            elif args.sectors_file:
                selection = {'sectors': load_json(Path(args.sectors_file), MAX_JSON)}
            else:
                selection = load_json(Path(args.codes_file), MAX_JSON)
            if not isinstance(selection, dict) or set(selection) - {'sectors', 'codes', 'provenance', 'universe'}:
                raise ValueError('invalid universe selection file')
            plan = create_plan(source, args.output_dir, args.dates, args.periods,
                shard_size=args.shard_size, max_seconds=args.max_seconds,
                allow_source_runtime_change=args.allow_source_runtime_change, **selection)
            result = {'state': 'complete', 'codes': len(plan['codes']), 'total_units': plan['total_units'],
                      'plan': str(Path(args.output_dir).resolve() / 'plan.json')}
        else:
            if args.progress_every < 1:
                raise ValueError('progress-every must be positive')
            def update(summary):
                if summary['run_units'] % args.progress_every == 0:
                    print(json.dumps(summary, ensure_ascii=True), flush=True)
            result = run_plan(source, args.output_dir, max_seconds=args.max_seconds,
                max_units=args.max_units, unit_seconds=args.unit_seconds, retry_failed=args.retry_failed,
                on_progress=update)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result['state'] == 'complete' else 1
    except Exception as exc:
        print(json.dumps({'state': 'incomplete', 'error': type(exc).__name__ + ': ' + str(exc)}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
