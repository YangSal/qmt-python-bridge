"""Consumer-owned historical archives, separate from bridge IPC and journals."""
import gzip
import hashlib
import json
import time
from pathlib import Path

from qmt_bridge.protocol import atomic_bytes, atomic_json as _atomic_json, json_value
from .kline import FIELDS, validate_kline


def write_json(path, value):
    """Publish consumer metadata with a bounded retry for transient file sharing.

    Only the local write is repeated, never a QMT call or download submission.
    Permanent denial still raises and leaves the previous atomic file intact.
    """
    deadline = time.monotonic() + .5
    while True:
        try:
            return _atomic_json(path, value)
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.01, remaining))


def write_bars(path, frame, code, period, date):
    """Validate the actual final read, then atomically save one bounded day."""
    evidence = validate_kline({code: frame}, code, period, date)
    rows = frame[['time'] + list(FIELDS)].sort_values('time').to_dict('records')
    payload = ''.join(json.dumps(dict(code=code, period=period, date=date,
                                      **json_value(row)), ensure_ascii=True,
                                  allow_nan=False, separators=(',', ':')) + '\n'
                      for row in rows).encode('utf-8')
    compressed = gzip.compress(payload, mtime=0)
    path = Path(path)
    atomic_bytes(path, compressed)
    return dict(evidence, bytes=len(compressed), sha256=hashlib.sha256(compressed).hexdigest())


def verify_bars(path, metadata):
    """Verify trusted local archive metadata without contacting QMT."""
    if (not isinstance(metadata, dict) or type(metadata.get('bytes')) is not int or
            not 0 < metadata['bytes'] <= 4 * 1024 * 1024 or
            type(metadata.get('rows')) is not int or metadata['rows'] < 1 or
            not isinstance(metadata.get('sha256'), str)):
        raise ValueError('invalid archive metadata')
    path = Path(path)
    if path.stat().st_size != metadata['bytes']:
        raise ValueError('archive size mismatch; original file preserved')
    with path.open('rb') as stream:
        raw = stream.read(metadata['bytes'] + 1)
    if hashlib.sha256(raw).hexdigest() != metadata['sha256']:
        raise ValueError('archive checksum mismatch; original file preserved')
    return True
