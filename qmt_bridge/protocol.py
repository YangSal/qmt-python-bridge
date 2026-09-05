"""Versioned, bounded JSON file protocol shared with the external client."""
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import uuid

PROTOCOL = 1
MAX_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
OPERATIONS = frozenset(('probe', 'market_data', 'divid_factors', 'financial',
                        'instrument', 'sectors', 'sector_stocks', 'weights'))


def check_id(request_id):
    if not isinstance(request_id, str) or not re.match(r'^[a-f0-9]{32}$', request_id):
        raise ValueError('invalid request_id')


def json_value(value):
    # No pandas/numpy import inside QMT; preserve structured rows and arrays.
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if hasattr(value, 'columns') and hasattr(value, 'to_dict'):
        return {'__frame__': True, 'columns': list(value.columns),
                'index': json_value(list(value.index)),
                'data': json_value(value.to_dict('records'))}
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if getattr(getattr(value, 'dtype', None), 'names', None):
        return [{k: json_value(row[k]) for k in value.dtype.names} for row in value]
    if hasattr(value, 'tolist'):
        return json_value(value.tolist())
    if hasattr(value, 'item'):
        return json_value(value.item())
    raise TypeError('unsupported QMT result type: %s' % type(value).__name__)


def dumps(value):
    return json.dumps(json_value(value), ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def atomic_bytes(path, data):
    path = str(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(temp, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json(path, value):
    atomic_bytes(path, dumps(value))


def load_json(path, limit=MAX_REQUEST_BYTES):
    with open(str(path), 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('JSON size limit exceeded')
    return json.loads(data.decode('utf-8'))


def publish_result(root, request_id, result, error):
    check_id(request_id)
    manifest = {'protocol': PROTOCOL, 'request_id': request_id, 'ok': error is None}
    if error is not None:
        manifest['error'] = str(error)[:2000]
    else:
        raw = dumps(result)
        if len(raw) > MAX_BYTES:
            raise ValueError('response exceeds 64 MiB; reduce batch/date window')
        data = gzip.compress(raw)
        atomic_bytes(os.path.join(root, request_id + '.json.gz'), data)
        manifest.update(sha256=hashlib.sha256(data).hexdigest(),
                        bytes=len(data), raw_bytes=len(raw))
    atomic_json(os.path.join(root, request_id + '.json'), manifest)


def read_result(root, request_id):
    check_id(request_id)
    manifest = load_json(os.path.join(root, request_id + '.json'))
    if manifest.get('protocol') != PROTOCOL or manifest.get('request_id') != request_id:
        raise ValueError('response protocol/request_id mismatch')
    if manifest.get('ok') is not True:
        raise ValueError(manifest.get('error', 'QMT request failed'))
    with open(os.path.join(root, request_id + '.json.gz'), 'rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or len(data) != manifest['bytes']:
        raise ValueError('response size mismatch')
    if hashlib.sha256(data).hexdigest() != manifest['sha256']:
        raise ValueError('response checksum mismatch')
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or len(raw) != manifest['raw_bytes']:
        raise ValueError('uncompressed response size mismatch')
    return json.loads(raw.decode('utf-8'))
