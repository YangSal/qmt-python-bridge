"""Bounded local file IPC for read-only QMT queries."""
import time
import uuid
from pathlib import Path

from qmt_bridge.protocol import (PROTOCOL, OPERATIONS, MAX_REQUEST_BYTES,
                                 atomic_json, dumps, read_result)
from . import QmtDataError


class FileTransport:
    def __init__(self, config):
        if not config.get('bridge_dir'):
            raise ValueError('bridge_dir is required')
        self.root = Path(config['bridge_dir']).resolve()
        self.timeout = float(config.get('timeout', 60))
        self.interval = float(config.get('poll_interval', .1))
        if self.timeout <= 0 or self.interval <= 0:
            raise ValueError('timeout and poll_interval must be positive')

    def call(self, operation, args):
        if operation not in OPERATIONS:
            raise QmtDataError('operation not allowed: ' + operation)
        rid = uuid.uuid4().hex
        request = {'protocol': PROTOCOL, 'request_id': rid, 'operation': operation,
                   'args': args, 'deadline': time.time() + self.timeout}
        if len(dumps(request)) > MAX_REQUEST_BYTES:
            raise QmtDataError('request too large')
        try:
            atomic_json(self.root / 'requests' / (rid + '.json'), request)
        except Exception as exc:
            raise QmtDataError('cannot publish QMT request: ' + rid) from exc
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if (self.root / 'responses' / (rid + '.json')).exists():
                try:
                    return read_result(str(self.root / 'responses'), rid)
                except Exception as exc:
                    raise QmtDataError('%s %s: %s' % (operation, rid, exc)) from exc
            time.sleep(min(self.interval, max(0, deadline - time.monotonic())))
        raise QmtDataError('QMT bridge timeout: %s %s; start the logged-in QMT strategy' % (operation, rid))
