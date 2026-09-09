"""Durable, at-most-once native downloads followed by strict cache verification."""
import copy
import datetime as dt
import time
from pathlib import Path

from qmt_bridge.auto_protocol import FileLock, PROTOCOL, request_hash, validate_download
from qmt_bridge.protocol import MAX_BYTES, atomic_json, check_id, load_json

from . import QmtDataError
from .config import validate_config
from .kline import FIELDS, validate_kline

ITEM_STATES = ('pending', 'running', 'awaiting_data', 'verified', 'incomplete', 'failed', 'unknown')


class QmtDownloadError(QmtDataError):
    def __init__(self, report):
        self.report = copy.deepcopy(report)
        super().__init__('K-line download job %s: %s' % (report['job_id'], report['state']))


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _scope(stock_list, period, start_time, end_time, expected_dates):
    try:
        if not isinstance(stock_list, (list, tuple)) or not stock_list:
            raise ValueError('stock_list must be a nonempty list')
        if not all(isinstance(code, str) for code in stock_list):
            raise ValueError('stock codes must be strings')
        codes = sorted(set(stock_list))
        if len(codes) > 10000:
            raise ValueError('stock_list limit is 10000')
        for code in codes:
            validate_download({'stock_code': code, 'period': period, 'date': start_time})
        validate_download({'stock_code': codes[0], 'period': period, 'date': end_time})
        start, end = dt.datetime.strptime(start_time, '%Y%m%d'), dt.datetime.strptime(end_time, '%Y%m%d')
        if not 0 <= (end - start).days < 366:
            raise ValueError('date interval must be ordered and at most 366 calendar days')
        if expected_dates is None:
            if start_time != end_time:
                raise ValueError('multi-day downloads require explicit expected_dates')
            dates = [start_time]
        else:
            if not isinstance(expected_dates, (list, tuple)) or not expected_dates:
                raise ValueError('expected_dates must be a nonempty ordered list')
            dates = list(expected_dates)
            for date in dates:
                validate_download({'stock_code': codes[0], 'period': period, 'date': date})
            if dates != sorted(set(dates)) or dates[0] != start_time or dates[-1] != end_time:
                raise ValueError('expected_dates must be unique, ordered and include both boundaries')
        if len(codes) * len(dates) > 20000:
            raise ValueError('download cell limit is 20000')
        return {'stock_list': codes, 'period': period, 'start_time': start_time,
                'end_time': end_time, 'expected_dates': dates}
    except (ValueError, TypeError) as exc:
        raise QmtDataError('invalid download scope: %s' % exc) from exc


def _item_id(job_id, request, code, date):
    return request_hash('download_job_item', {'job_id': job_id, 'stock_code': code,
                        'period': request['period'], 'date': date})[:32]


class DownloadManager:
    def __init__(self, transport, config):
        validate_config(config)
        self.transport = transport
        self.root = Path(config.get('job_dir') or Path(config['bridge_dir']) / 'client_jobs').resolve()
        self.timeout = float(config.get('download_timeout', 120))
        self.interval = float(config.get('poll_interval', .1))

    def _path(self, job_id):
        try:
            check_id(job_id)
            if len(job_id) != 32:
                raise ValueError('job_id must have exactly 32 hexadecimal characters')
        except ValueError as exc:
            raise QmtDataError('invalid job_id') from exc
        return self.root / (job_id + '.json')

    def status(self, job_id):
        """Read the local report without probing or advancing any request."""
        path = self._path(job_id)
        try:
            report = load_json(path, MAX_BYTES)
            request = report['request']
            expected = [(code, date) for code in request['stock_list'] for date in request['expected_dates']]
            if report['job_id'] != job_id or len(report['items']) != len(expected):
                raise ValueError('job identity/item count mismatch')
            normalized = _scope(**request)
            if normalized != request:
                raise ValueError('stored job scope is not normalized')
            for item, (code, date) in zip(report['items'], expected):
                if (item['code'] != code or item['date'] != date or
                        item['request_id'] != _item_id(job_id, request, code, date) or
                        item['state'] not in ITEM_STATES or type(item['attempted']) is not bool):
                    raise ValueError('stored job item identity/state mismatch')
            return report
        except Exception as exc:
            raise QmtDataError('cannot read valid download job %s: %s' % (job_id, exc)) from exc

    def _save(self, report):
        counts = {state: sum(item['state'] == state for item in report['items']) for state in ITEM_STATES}
        counts['total'] = len(report['items'])
        report['totals'] = counts
        if counts['unknown']:
            state = 'unknown'
        elif counts['verified'] == counts['total']:
            state = 'verified'
        elif counts['running'] or counts['awaiting_data']:
            state = 'running'
        elif counts['pending']:
            state = 'pending'
        elif counts['verified']:
            state = 'partial'
        elif counts['failed']:
            state = 'failed'
        else:
            state = 'incomplete'
        report['state'], report['updated_at'] = state, _now()
        atomic_json(self._path(report['job_id']), report)

    def _transition(self, report, item, state, error=None, validation=None):
        item.update(state=state, error=None if error is None else str(error)[:2000],
                    validation=validation, updated_at=_now())
        self._save(report)

    def _read(self, request, item, deadline):
        if time.monotonic() >= deadline:
            raise QmtDataError('cell cache verification deadline exceeded')
        args = {'fields': ['time'] + list(FIELDS), 'stock_list': [item['code']],
                'period': request['period'], 'start_time': item['date'], 'end_time': item['date'],
                'count': -1, 'dividend_type': 'none', 'fill_data': False, 'subscribe': False}
        request_id = self.transport.submit('market_data', args)
        remaining = max(.000001, deadline - time.monotonic())
        response = self.transport.wait(request_id, timeout=remaining)
        if response['state'] != 'returned':
            raise QmtDataError('cache query %s: %s' % (request_id, response.get('error') or response['state']))
        return response['data']

    def _cached(self, report, item, deadline):
        raw = self._read(report['request'], item, deadline)
        try:
            validation = validate_kline(raw, item['code'], report['request']['period'], item['date'])
        except QmtDataError as exc:
            return None, str(exc)
        return validation, None

    def _advance(self, report, item):
        deadline = time.monotonic() + self.timeout
        try:
            if item['attempted']:
                # Corrupt/mismatched native evidence cannot be hidden by a warm
                # cache. Unknown evidence can still reconcile via strict data.
                original = self.transport.lookup(item['request_id'])
                expected_hash = request_hash('download_kline', {'stock_code': item['code'],
                    'period': report['request']['period'], 'date': item['date']})
                if original.get('args_hash') not in (None, expected_hash):
                    raise QmtDataError('download request identity differs from job scope')
            validation, error = self._cached(report, item, deadline)
        except Exception as exc:
            self._transition(report, item, 'unknown', exc)
            return
        if validation is not None:
            self._transition(report, item, 'verified', validation=validation)
            return
        # Save the attempted bit BEFORE publishing: a crash here is uncertain and
        # must never generate another native invocation on resume.
        if not item['attempted']:
            if time.monotonic() >= deadline:
                self._transition(report, item, 'incomplete', 'cell deadline exceeded before download')
                return
            item['attempted'] = True
            self._transition(report, item, 'running', error)
            try:
                if time.monotonic() >= deadline:
                    raise QmtDataError('cell deadline exceeded before download publication')
                self.transport.submit('download_kline', {'stock_code': item['code'],
                    'period': report['request']['period'], 'date': item['date']},
                    request_id=item['request_id'])
            except Exception as exc:
                self._transition(report, item, 'unknown', exc)
                return
        try:
            remaining = max(.000001, deadline - time.monotonic())
            response = self.transport.wait(item['request_id'], timeout=remaining)
        except Exception as exc:
            self._transition(report, item, 'unknown', exc)
            return
        if response['state'] in ('failed', 'expired'):
            self._transition(report, item, 'failed', response.get('error') or response['state'])
            return
        if response['state'] != 'returned':
            self._transition(report, item, 'unknown', response.get('error') or response['state'])
            return
        self._transition(report, item, 'awaiting_data', error)
        while time.monotonic() < deadline:
            try:
                validation, error = self._cached(report, item, deadline)
            except Exception as exc:
                # The native call has a known returned result; read failure does
                # not erase that evidence or authorize another download.
                self._transition(report, item, 'incomplete', exc)
                return
            if validation is not None:
                self._transition(report, item, 'verified', validation=validation)
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.interval, remaining))
        self._transition(report, item, 'incomplete', error or 'cache verification deadline exceeded')

    def download(self, stock_list, period, start_time, end_time, expected_dates=None,
                 job_id=None, callback=None):
        request = _scope(stock_list, period, start_time, end_time, expected_dates)
        if callback is not None and not callable(callback):
            raise QmtDataError('callback must be callable')
        job_id = request_hash('download_job', request)[:32] if job_id is None else job_id
        path = self._path(job_id)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            lock = FileLock(self.root / (job_id + '.lock'))
        except Exception as exc:
            raise QmtDataError('download job is locked: ' + job_id) from exc
        with lock:
            if path.exists():
                report = self.status(job_id)
                if report['request'] != request:
                    raise QmtDataError('job_id is already bound to different scope parameters')
            else:
                now = _now()
                report = {'job_id': job_id, 'request': request, 'created_at': now,
                          'items': [{'code': code, 'date': date,
                                     'request_id': _item_id(job_id, request, code, date),
                                     'state': 'pending', 'attempted': False, 'error': None,
                                     'validation': None, 'updated_at': now}
                                    for code in request['stock_list'] for date in request['expected_dates']]}
                self._save(report)
            probe = self.transport.call('probe', {})
            if (not isinstance(probe, dict) or probe.get('automatic_kline') != PROTOCOL or
                    type(probe.get('worker_version')) is not int or probe['worker_version'] < 3 or
                    probe.get('downloads_enabled') is not True):
                raise QmtDataError('worker does not enable automatic K-line protocol ' + PROTOCOL)
            # Reconcile attempted cells first, even if a prior crash left earlier
            # unattempted cells. Never enlarge an unresolved native queue.
            ordered = sorted(report['items'], key=lambda item: not item['attempted'])
            for item in ordered:
                self._advance(report, item)
                if callback is not None:
                    callback(copy.deepcopy(report))
                if item['state'] == 'unknown':
                    break
            if report['state'] != 'verified':
                raise QmtDownloadError(report)
            return copy.deepcopy(report)
