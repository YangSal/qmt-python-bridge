"""Versioned automatic-download protocol shared by QMT and its client."""
import datetime
import hashlib
import json
import math
import os
import re

from . import protocol as _old

PROTOCOL = 'auto-kline-v1'
OPERATIONS = _old.OPERATIONS | frozenset(('download_kline',))


def request_hash(operation, args):
    """Bind an operation and its arguments using canonical JSON."""
    if not isinstance(operation, str) or not isinstance(args, dict):
        raise ValueError('operation must be a string and args must be an object')
    value = {'operation': operation, 'args': _old.json_value(args)}
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class FileLock(object):
    """A process-scoped, nonblocking lock backed by a one-byte OS lock."""
    def __init__(self, path):
        self.path = os.path.abspath(str(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._stream = open(self.path, 'a+b')
        try:
            self._stream.seek(0)
            if not self._stream.read(1):
                self._stream.seek(0)
                self._stream.write(b'0')
                self._stream.flush()
                os.fsync(self._stream.fileno())
            self._stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self._stream.close()
            self._stream = None
            raise

    def __enter__(self):
        if self._stream is None:
            raise RuntimeError('file lock is closed')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None


def validate_download(args):
    """Validate one stock, one supported period and one finished Beijing day."""
    if not isinstance(args, dict) or set(args) != {'stock_code', 'period', 'date'}:
        raise ValueError('download args must contain stock_code, period and date only')
    stock_code, period, date = args['stock_code'], args['period'], args['date']
    if not isinstance(stock_code, str) or not re.match(r'^[0-9]{6}\.(SH|SZ|BJ)$', stock_code):
        raise ValueError('invalid stock_code')
    if period not in ('1d', '1m', '5m'):
        raise ValueError('unsupported download period')
    if not isinstance(date, str) or not re.match(r'^[0-9]{8}$', date):
        raise ValueError('date must be YYYYMMDD')
    try:
        requested = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:]))
    except ValueError:
        raise ValueError('date must be a valid YYYYMMDD date')
    beijing_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    if requested >= beijing_today:
        raise ValueError('download date must be earlier than Beijing today')
    return stock_code, period, date
