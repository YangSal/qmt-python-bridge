"""Durable one-request-per-poll worker for the automatic K-line bridge."""
import math
import os
import time
import uuid

from .auto_protocol import (FileLock, OPERATIONS, PROTOCOL, request_hash,
                            validate_download)
from .protocol import (MAX_BYTES, MAX_REQUEST_BYTES, atomic_json, check_id,
                       load_json, publish_result, read_result)
from .worker import dispatch as readonly_dispatch


class AutomaticWorker(object):
    def __init__(self, root, context, api, downloads_enabled=False):
        self.root = os.path.abspath(root)
        self.context = context
        self.api = api
        self.downloads_enabled = downloads_enabled is True
        self._lock = None
        for name in ('requests', 'running', 'responses', 'records', 'states',
                     'client_jobs'):
            os.makedirs(os.path.join(self.root, name), exist_ok=True)
        self._lock = FileLock(os.path.join(self.root, 'worker.lock'))
        try:
            self._recover_running()
        except Exception:
            self.close()
            raise

    def close(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _path(self, directory, request_id):
        return os.path.join(self.root, directory, request_id + '.json')

    def _quarantine(self, path):
        directory = os.path.join(self.root, 'quarantine')
        os.makedirs(directory, exist_ok=True)
        os.replace(path, os.path.join(directory, uuid.uuid4().hex + '.json'))

    def _load_record(self, request_id):
        record = load_json(self._path('records', request_id), MAX_REQUEST_BYTES)
        if (not isinstance(record, dict) or record.get('protocol') != PROTOCOL or
                record.get('request_id') != request_id or
                record.get('operation') not in OPERATIONS or
                not isinstance(record.get('args'), dict)):
            raise ValueError('record protocol/request identity mismatch')
        expected = request_hash(record['operation'], record['args'])
        if record.get('args_hash') != expected:
            raise ValueError('record args_hash mismatch')
        return record

    def _validate_envelope(self, request_id, envelope):
        if (not isinstance(envelope, dict) or
                envelope.get('auto_protocol') != PROTOCOL or
                envelope.get('request_id') != request_id or
                envelope.get('state') not in ('returned', 'failed', 'expired', 'unknown') or
                'args_hash' not in envelope or 'data' not in envelope or
                'error' not in envelope):
            raise ValueError('automatic response envelope is invalid')
        return envelope

    def _terminal(self, request_id, args_hash, state, data, error, late=None):
        envelope = {'auto_protocol': PROTOCOL, 'request_id': request_id,
                    'args_hash': args_hash, 'state': state, 'data': data,
                    'error': None if error is None else str(error)[:2000]}
        if late is not None:
            envelope['late'] = bool(late)
        atomic_json(self._path('states', request_id), envelope)
        try:
            publish_result(os.path.join(self.root, 'responses'), request_id,
                           envelope, None)
        except Exception:
            # The state journal is authoritative. A later poll repairs the
            # response without changing or replaying the completed operation.
            pass
        return envelope

    def _repair_response(self):
        states = os.path.join(self.root, 'states')
        for name in sorted(name for name in os.listdir(states)
                           if name.endswith('.json')):
            request_id = name[:-5]
            response_path = self._path('responses', request_id)
            if os.path.exists(response_path):
                continue
            try:
                check_id(request_id)
                envelope = self._validate_envelope(
                    request_id, load_json(os.path.join(states, name), MAX_BYTES))
                publish_result(os.path.join(self.root, 'responses'), request_id,
                               envelope, None)
                return True
            except Exception:
                # Preserve corrupt state evidence. A transient response failure
                # is retried by the next timer callback.
                continue
        return False

    def _recover_running(self):
        directory = os.path.join(self.root, 'running')
        for name in sorted(os.listdir(directory)):
            if not name.endswith('.json'):
                continue
            path = os.path.join(directory, name)
            request_id = name[:-5]
            try:
                check_id(request_id)
            except ValueError:
                self._quarantine(path)
                continue
            state_path = self._path('states', request_id)
            response_path = self._path('responses', request_id)
            try:
                if os.path.exists(state_path):
                    envelope = self._validate_envelope(
                        request_id, load_json(state_path, MAX_BYTES))
                    if not os.path.exists(response_path):
                        publish_result(os.path.join(self.root, 'responses'),
                                       request_id, envelope, None)
                elif os.path.exists(response_path):
                    envelope = self._validate_envelope(
                        request_id,
                        read_result(os.path.join(self.root, 'responses'), request_id))
                    atomic_json(state_path, envelope)
                else:
                    record = self._load_record(request_id)
                    self._terminal(request_id, record['args_hash'], 'unknown', None,
                                   'worker stopped while the request was running')
            except Exception:
                # A corrupt authoritative journal must neither block startup nor
                # permit replay. Leave it in place so the client reports it.
                pass
            finally:
                if os.path.exists(path):
                    os.unlink(path)

    def _dispatch(self, operation, args):
        if operation == 'probe':
            result = readonly_dispatch(self.context, self.api, operation, args)
            result['automatic_kline'] = PROTOCOL
            result['downloads_enabled'] = self.downloads_enabled
            result['worker_version'] = 3
            return result
        if operation != 'download_kline':
            return readonly_dispatch(self.context, self.api, operation, args)
        if not self.downloads_enabled:
            raise ValueError('automatic K-line downloads are disabled')
        stock_code, period, date = validate_download(args)
        function = self.api.get('download_history_data')
        api_name = 'download_history_data'
        if not callable(function):
            function = self.api.get('down_history_data')
            api_name = 'down_history_data'
        if not callable(function):
            raise ValueError('QMT global download function is unavailable')
        started = time.monotonic()
        return_value = function(stock_code, period, date, date)
        elapsed = time.monotonic() - started
        negative = (isinstance(return_value, (int, float)) and
                    not isinstance(return_value, bool) and return_value < 0)
        if return_value is False or negative:
            raise ValueError('%s reported download failure: %r' %
                             (api_name, return_value))
        return {'api': api_name, 'data_ready': False,
                'return_value': return_value, 'elapsed_seconds': elapsed}

    def poll(self):
        if self._lock is None:
            raise RuntimeError('worker is closed')
        if self._repair_response():
            return True
        requests = os.path.join(self.root, 'requests')
        names = sorted(name for name in os.listdir(requests)
                       if name.endswith('.json'))
        if not names:
            return False
        name = names[0]
        request_id = name[:-5]
        source = os.path.join(requests, name)
        try:
            check_id(request_id)
        except ValueError:
            self._quarantine(source)
            return True
        running = self._path('running', request_id)
        os.replace(source, running)
        state_path = self._path('states', request_id)
        response_path = self._path('responses', request_id)
        try:
            if os.path.exists(state_path):
                try:
                    envelope = self._validate_envelope(
                        request_id, load_json(state_path, MAX_BYTES))
                    if not os.path.exists(response_path):
                        publish_result(os.path.join(self.root, 'responses'), request_id,
                                       envelope, None)
                except Exception:
                    # Preserve corrupt state as evidence and never dispatch again.
                    pass
                return True
            if os.path.exists(response_path):
                try:
                    envelope = self._validate_envelope(
                        request_id,
                        read_result(os.path.join(self.root, 'responses'), request_id))
                    atomic_json(state_path, envelope)
                except Exception:
                    # A response artifact proves prior processing. Preserve it as
                    # evidence, consume the duplicate queue entry and never replay.
                    pass
                return True
            record = None
            try:
                record = self._load_record(request_id)
                request = load_json(running, MAX_REQUEST_BYTES)
                if (not isinstance(request, dict) or
                        request.get('protocol') != PROTOCOL or
                        request.get('request_id') != request_id or
                        request.get('operation') != record['operation'] or
                        request.get('args') != record['args'] or
                        request_hash(request.get('operation'),
                                     request.get('args')) != record['args_hash']):
                    raise ValueError('request protocol/record identity mismatch')
                deadline = float(request['deadline'])
                if not math.isfinite(deadline):
                    raise ValueError('deadline must be finite')
                if time.time() >= deadline:
                    self._terminal(request_id, record['args_hash'], 'expired', None,
                                   'request expired before execution')
                    return True
                result = self._dispatch(record['operation'], record['args'])
                self._terminal(request_id, record['args_hash'], 'returned', result,
                               None, late=time.time() >= deadline)
            except Exception as exc:
                args_hash = record['args_hash'] if record is not None else None
                self._terminal(request_id, args_hash, 'failed', None,
                               '%s: %s' % (type(exc).__name__, exc))
        finally:
            if os.path.exists(running):
                os.unlink(running)
        return True
