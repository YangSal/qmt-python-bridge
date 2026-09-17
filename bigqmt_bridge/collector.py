"""Run as an external bridge user: download, validate and archive small samples."""
import argparse
import datetime as dt
import json
import math
import time
from pathlib import Path

from qmt_bridge.auto_protocol import FileLock, validate_download
from qmt_bridge.protocol import load_json
from .archive import write_bars, verify_bars, write_json as atomic_json
from .auto_backend import AutomaticBackend
from .backend import create_backend
from .config import load_config
from .downloads import QmtDownloadError


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _scope(codes, periods, dates):
    for name, values, limit in [('codes', codes, 10), ('periods', periods, 3),
                                 ('dates', dates, 31)]:
        if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= limit or
                any(not isinstance(value, str) for value in values) or
                len(set(values)) != len(values)):
            raise ValueError('%s must be unique nonempty strings (limit %s)' % (name, limit))
    if list(dates) != sorted(dates):
        raise ValueError('dates must be ordered explicit trading dates')
    for code in codes:
        for period in periods:
            for date in dates:
                validate_download({'stock_code': code, 'period': period, 'date': date})
    return {'codes': sorted(codes), 'periods': sorted(periods), 'dates': list(dates)}


def _cell_path(root, item):
    path = root / 'history' / item['period'] / item['code'] / (item['date'] + '.jsonl.gz')
    if not path.resolve().is_relative_to(root):
        raise ValueError('archive path must stay inside output directory')
    return path


def _save(root, report):
    report['updated_at_utc'] = _now()
    report['saved_cells'] = sum(item['state'] == 'saved' for item in report['items'])
    atomic_json(root / 'collection.json', report)


def _manifest(root, scope, source):
    identities = [{'code': code, 'period': period, 'date': date}
                  for date in scope['dates'] for period in scope['periods'] for code in scope['codes']]
    origin = {'bridge_dir': str(source.transport.root), 'job_dir': str(source.downloads.root)}
    path = root / 'collection.json'
    if path.exists():
        report = load_json(path, 8 * 1024 * 1024)
        if (not isinstance(report, dict) or report.get('schema') != 'disk-history-v1' or
                report.get('scope') != scope or report.get('source') != origin):
            raise ValueError('collection scope/source mismatch; choose a separate output directory')
        items = report.get('items')
        if not isinstance(items, list) or len(items) != len(identities):
            raise ValueError('invalid collection manifest items')
        for item, identity in zip(items, identities):
            if (not isinstance(item, dict) or any(item.get(k) != v for k, v in identity.items()) or
                    item.get('state') not in ('pending', 'running', 'saved', 'incomplete') or
                    'file' not in item or 'download' not in item):
                raise ValueError('invalid collection item identity/state')
            if item['state'] == 'saved' and not isinstance(item['file'], dict):
                raise ValueError('saved collection item has no file evidence')
        return report
    return {'schema': 'disk-history-v1', 'state': 'pending', 'scope': scope, 'source': origin,
            'created_at_utc': _now(), 'errors': [], 'items': [dict(identity, state='pending',
                file=None, download=None, action=None, error=None) for identity in identities]}


def collect_history(source, codes, periods, dates, output_dir, max_seconds=300):
    """Collect with exclusive use of an AutomaticBackend; resume local files by hash.

    One output directory is bound to one scope and source. Native request IDs and
    unknown-state reconciliation belong to the existing DownloadManager journal.
    The budget bounds new work and external waits, not embedded-call cancellation.
    """
    scope = _scope(codes, periods, dates)
    if not isinstance(source, AutomaticBackend):
        raise ValueError('history collection requires the automatic QMT backend')
    if type(max_seconds) not in (float, int) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('max_seconds must be finite and positive')
    root = Path(output_dir).resolve()
    for runtime in (source.transport.root, source.downloads.root):
        if root.is_relative_to(runtime) or runtime.is_relative_to(root):
            raise ValueError('output directory must be separate from bridge IPC/job directories')
    deadline = time.monotonic() + max_seconds
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / 'collector.lock'):
        report = _manifest(root, scope, source)
        report.update(state='running', errors=[])
        _save(root, report)
        original_timeout, original_download_timeout = source.transport.timeout, source.downloads.timeout
        try:
            # Check the entire local archive before recovery can contact QMT.
            # A later corrupt file must not be hidden behind earlier missing files.
            for item in report['items']:
                try:
                    path = _cell_path(root, item)
                    if item['file'] is not None and path.exists():
                        verify_bars(path, item['file'])
                except Exception as exc:
                    error = ('%s: %s' % (type(exc).__name__, exc))[:2000]
                    item.update(state='incomplete', error=error)
                    report.update(state='incomplete', errors=[error])
                    _save(root, report)
                    return report
            for item in report['items']:
                try:
                    path = _cell_path(root, item)
                    if item['file'] is not None and path.exists():
                        verify_bars(path, item['file'])
                        item.update(state='saved', action='local_verified', error=None)
                        _save(root, report)
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('collection budget exhausted; resume the same output directory')
                    item.update(state='running', error=None, file=None, action='qmt_read')
                    _save(root, report)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('collection budget exhausted during progress publication')
                    # A download first probes, then runs one cell with its own deadline.
                    # Reserve at most half the remaining budget for each part.
                    source.transport.timeout = min(original_timeout, remaining / 2)
                    source.downloads.timeout = min(original_download_timeout, remaining / 2)
                    previous = item['download']
                    job_id = previous['job_id'] if previous is not None else None
                    try:
                        item['download'] = source.download_history_data2(
                            [item['code']], item['period'], item['date'], item['date'], job_id=job_id)
                    except QmtDownloadError as exc:
                        item['download'] = exc.report
                        raise
                    _save(root, report)
                    if item['download']['state'] != 'verified' or item['download'].get('errors'):
                        raise ValueError('download has not been freshly verified')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('collection budget exhausted before final archive read')
                    source.transport.timeout = min(original_timeout, remaining)
                    frames = source.get_market_data_ex(
                        ['time', 'open', 'high', 'low', 'close', 'volume', 'amount'],
                        [item['code']], item['period'], item['date'], item['date'],
                        count=-1, dividend_type='none', fill_data=False, subscribe=False)
                    item['file'] = write_bars(path, frames[item['code']], item['code'],
                                              item['period'], item['date'])
                    item.update(state='saved', error=None)
                    _save(root, report)
                except Exception as exc:
                    error = ('%s: %s' % (type(exc).__name__, exc))[:2000]
                    item.update(state='incomplete', error=error)
                    report.update(state='incomplete', errors=[error])
                    _save(root, report)
                    return report
            report.update(state='complete', errors=[])
            _save(root, report)
            return report
        finally:
            source.transport.timeout = original_timeout
            source.downloads.timeout = original_download_timeout


def realtime_check(output_dir):
    """This bridge version has no accepted real-time transport or client API."""
    report = {'schema': 'disk-realtime-check-v1', 'state': 'incomplete',
              'checked_at_utc': _now(), 'transport_verified': False,
              'received_messages': 0, 'reason': 'realtime_bridge_not_implemented',
              'next_step': 'Complete M1b transport qualification and M2 market-data API; '
                           'historical file IPC cannot provide this real-time test.'}
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / 'collector.lock'):
        atomic_json(root / 'realtime-check.json', report)
    return report


def _csv(value):
    return [part.strip() for part in value.split(',') if part.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    history = subs.add_parser('history', help='download/read and archive historical K-lines')
    history.add_argument('--config', required=True)
    history.add_argument('--codes', type=_csv, required=True)
    history.add_argument('--dates', type=_csv, required=True, help='explicit ordered Beijing trading dates')
    history.add_argument('--periods', type=_csv, default=['1d', '1m', '5m'])
    history.add_argument('--output-dir', required=True)
    history.add_argument('--max-seconds', type=float, default=300)
    realtime = subs.add_parser('realtime-check', help='report the current missing real-time bridge capability')
    realtime.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'history':
            # Reject bad scope/config before creating transport directories or requests.
            _scope(args.codes, args.periods, args.dates)
            config = load_config(args.config)
            if config.get('history_mode') != 'auto' or config.get('backend', 'file_bridge') != 'file_bridge':
                raise ValueError('collector requires explicit history_mode=auto and file_bridge backend')
            report = collect_history(create_backend(config), args.codes, args.periods, args.dates,
                                     args.output_dir, args.max_seconds)
            filename = 'collection.json'
        else:
            report = realtime_check(args.output_dir)
            filename = 'realtime-check.json'
        print(json.dumps({'state': report['state'], 'saved_cells': report.get('saved_cells', 0),
                          'report': str(Path(args.output_dir).resolve() / filename),
                          'errors': report.get('errors', []), 'reason': report.get('reason')}, ensure_ascii=True))
        return 0 if report['state'] == 'complete' else 1
    except Exception as exc:
        # Do not overwrite an existing collection manifest on scope/lock/config failure.
        print(json.dumps({'state': 'incomplete', 'errors': [type(exc).__name__ + ': ' + str(exc)]},
                         ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
