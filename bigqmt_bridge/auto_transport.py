"""Bounded durable client transport for the automatic K-line worker."""
import math
import os
import time
import uuid
from pathlib import Path

from qmt_bridge.auto_protocol import (FileLock, OPERATIONS, PROTOCOL,
                                      request_hash, validate_download)
from qmt_bridge.protocol import (MAX_BYTES, MAX_REQUEST_BYTES, atomic_json,
                                 check_id, dumps, load_json, read_result)
from . import QmtDataError


class QmtRequestTimeout(QmtDataError):
    def __init__(self, request_id, message=None):
        self.request_id = request_id
        super().__init__(message or ('QMT bridge request timed out: ' + request_id))


class AutomaticTransport(object):
    def __init__(self, config):
        if not isinstance(config, dict) or not config.get('bridge_dir'):
            raise ValueError('bridge_dir is required')
        self.root = Path(config['bridge_dir']).resolve()
        self.timeout = float(config.get('timeout', 60))
        self.interval = float(config.get('poll_interval', .1))
        if (self.timeout <= 0 or self.interval <= 0 or
                not math.isfinite(self.timeout) or
                not math.isfinite(self.interval)):
            raise ValueError('timeout and poll_interval must be positive')
        for name in ('requests', 'running', 'responses', 'records', 'states',
                     'client_jobs'):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _record_path(self, request_id):
        return self.root / 'records' / (request_id + '.json')

    def _validate_record(self, request_id, record):
        if (not isinstance(record, dict) or record.get('protocol') != PROTOCOL or
                record.get('request_id') != request_id or
                record.get('operation') not in OPERATIONS or
                not isinstance(record.get('args'), dict)):
            raise ValueError('record protocol/request identity mismatch')
        expected = request_hash(record['operation'], record['args'])
        if record.get('args_hash') != expected:
            raise ValueError('record args_hash mismatch')
        return record

    def _load_record(self, request_id):
        return self._validate_record(
            request_id, load_json(self._record_path(request_id), MAX_REQUEST_BYTES))

    def submit(self, operation, args, request_id=None):
        if operation not in OPERATIONS:
            raise QmtDataError('operation not allowed: ' + str(operation))
        if not isinstance(args, dict):
            raise QmtDataError('args must be an object')
        try:
            if operation == 'download_kline':
                validate_download(args)
            request_id = uuid.uuid4().hex if request_id is None else request_id
            check_id(request_id)
            args_hash = request_hash(operation, args)
        except Exception as exc:
            raise QmtDataError('invalid automatic QMT request: %s' % exc) from exc
        record = {'protocol': PROTOCOL, 'request_id': request_id,
                  'operation': operation, 'args': args, 'args_hash': args_hash}
        request = {'protocol': PROTOCOL, 'request_id': request_id,
                   'operation': operation, 'args': args,
                   'deadline': time.time() + self.timeout}
        if len(dumps(record)) > MAX_REQUEST_BYTES or len(dumps(request)) > MAX_REQUEST_BYTES:
            raise QmtDataError('request too large')
        try:
            with FileLock(self.root / 'client.lock'):
                record_path = self._record_path(request_id)
                if record_path.exists():
                    existing = self._load_record(request_id)
                    if (existing['operation'] != operation or
                            existing['args_hash'] != args_hash or
                            existing['args'] != args):
                        raise QmtDataError('request_id is already bound to different arguments')
                    return request_id
                artifacts = [self.root / directory / (request_id + '.json')
                             for directory in ('requests', 'running', 'states', 'responses')]
                if any(path.exists() for path in artifacts):
                    raise QmtDataError('request artifacts exist without immutable record')
                pending = set()
                for directory in ('requests', 'running'):
                    pending.update(path.name for path in (self.root / directory).glob('*.json'))
                if len(pending) >= 32:
                    raise QmtDataError('automatic QMT queue limit is 32')
                atomic_json(record_path, record)
                atomic_json(self.root / 'requests' / (request_id + '.json'), request)
        except QmtDataError:
            raise
        except Exception as exc:
            raise QmtDataError('cannot publish automatic QMT request: ' + request_id) from exc
        return request_id

    def _validate_envelope(self, request_id, envelope, record):
        if (not isinstance(envelope, dict) or
                envelope.get('auto_protocol') != PROTOCOL or
                envelope.get('request_id') != request_id or
                envelope.get('state') not in ('returned', 'failed', 'expired', 'unknown') or
                'args_hash' not in envelope or 'data' not in envelope or
                'error' not in envelope):
            raise ValueError('automatic response envelope is invalid')
        if record is not None and envelope.get('args_hash') != record['args_hash']:
            raise ValueError('automatic response args_hash mismatch')
        return envelope

    def _status(self, request_id, args_hash, state, error=None):
        return {'auto_protocol': PROTOCOL, 'request_id': request_id,
                'args_hash': args_hash, 'state': state, 'data': None,
                'error': error}

    def _read_terminal(self, request_id, record, state_path, response_path):
        state = None
        if state_path.exists():
            state = self._validate_envelope(
                request_id, load_json(state_path, MAX_BYTES), record)
        response = None
        if response_path.exists():
            response = self._validate_envelope(
                request_id, read_result(str(self.root / 'responses'), request_id),
                record)
        if state is not None and response is not None and state != response:
            raise ValueError('server state and response disagree')
        return state if state is not None else response

    def lookup(self, request_id):
        try:
            check_id(request_id)
        except Exception as exc:
            raise QmtDataError('invalid request_id') from exc
        record = None
        record_path = self._record_path(request_id)
        try:
            if record_path.exists():
                record = self._load_record(request_id)
            state_path = self.root / 'states' / (request_id + '.json')
            response_path = self.root / 'responses' / (request_id + '.json')
            queued_paths = [self.root / directory / (request_id + '.json')
                            for directory in ('requests', 'running')]
            if record is None and (response_path.exists() or state_path.exists() or
                                   any(path.exists() for path in queued_paths)):
                raise ValueError('immutable request record is missing')
            terminal = self._read_terminal(
                request_id, record, state_path, response_path)
            if terminal is not None:
                return terminal
        except Exception as exc:
            raise QmtDataError('corrupt automatic QMT journal for %s: %s' %
                               (request_id, exc)) from exc
        args_hash = record['args_hash'] if record is not None else None
        queued = any((self.root / directory / (request_id + '.json')).exists()
                     for directory in ('requests', 'running'))
        if queued:
            return self._status(request_id, args_hash, 'pending')
        try:
            terminal = self._read_terminal(
                request_id, record,
                self.root / 'states' / (request_id + '.json'),
                self.root / 'responses' / (request_id + '.json'))
            if terminal is not None:
                return terminal
        except Exception as exc:
            raise QmtDataError('corrupt automatic QMT journal for %s: %s' %
                               (request_id, exc)) from exc
        return self._status(request_id, args_hash, 'unknown',
                            'no queued request or terminal server journal')

    def wait(self, request_id, timeout=None):
        timeout = self.timeout if timeout is None else float(timeout)
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError('timeout must be positive')
        deadline = time.monotonic() + timeout
        while True:
            result = self.lookup(request_id)
            if result['state'] != 'pending':
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QmtRequestTimeout(request_id)
            time.sleep(min(self.interval, remaining))

    def call(self, operation, args):
        request_id = self.submit(operation, args)
        result = self.wait(request_id)
        if result['state'] == 'returned':
            return result['data']
        raise QmtDataError('%s %s: %s' %
                           (operation, request_id,
                            result.get('error') or result['state']))
