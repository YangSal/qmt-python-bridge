"""Python 3.6-compatible bounded memory protocol. No market file serialization."""
import hashlib
import hmac
import json
import math
import os
import struct
import sys

PROTOCOL = 'qmt-memory-v1'
MAX_FRAME = 262144


def check_id(value):
    if (not isinstance(value, str) or len(value) != 32 or
            any(c not in '0123456789abcdef' for c in value)):
        raise ValueError('invalid session/challenge identity')
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('nonfinite JSON value')


def _number(value, converter):
    if len(value) > 64:
        raise ValueError('JSON number length limit exceeded')
    result = converter(value)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError('nonfinite JSON number')
    return result


def auth_proof(key, role, *parts):
    if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('a 256-bit authentication key is required')
    return hmac.new(bytes.fromhex(key), '|'.join((role,) + parts).encode('ascii'),
                    hashlib.sha256).hexdigest()


def _validate(message):
    if not isinstance(message, dict) or set(message) != {'protocol', 'session', 'seq', 'kind', 'body'}:
        raise ValueError('invalid memory envelope')
    if message['protocol'] != PROTOCOL:
        raise ValueError('protocol mismatch')
    check_id(message['session'])
    if type(message['seq']) is not int or not 1 <= message['seq'] <= 2**53:
        raise ValueError('invalid sequence')
    if not isinstance(message['kind'], str) or not 1 <= len(message['kind']) <= 32:
        raise ValueError('invalid message kind')
    if not isinstance(message['body'], dict):
        raise ValueError('message body must be an object')
    return message


def encode(session, seq, kind, body):
    message = _validate({'protocol': PROTOCOL, 'session': session, 'seq': seq,
                         'kind': kind, 'body': body})
    raw = json.dumps(message, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':'), sort_keys=True).encode('utf-8')
    if len(raw) + 40 > MAX_FRAME:
        raise ValueError('memory frame exceeds length limit')
    return b'QMP1' + struct.pack('!I', len(raw)) + hashlib.sha256(raw).digest() + raw


def decode(frame):
    if not isinstance(frame, bytes) or not 40 < len(frame) <= MAX_FRAME or frame[:4] != b'QMP1':
        raise ValueError('invalid memory frame length/magic')
    length = struct.unpack('!I', frame[4:8])[0]
    if length != len(frame) - 40:
        raise ValueError('truncated or excessive memory frame')
    raw = frame[40:]
    if hashlib.sha256(raw).digest() != frame[8:40]:
        raise ValueError('memory frame checksum mismatch')
    return _validate(json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                                parse_constant=_invalid_constant,
                                parse_float=lambda value: _number(value, float),
                                parse_int=lambda value: _number(value, int)))


class EmbeddedSession(object):
    """One connection: both peers prove freshness before any application call."""
    def __init__(self, run_id, auth_key, application=None):
        self.run_id = check_id(run_id)
        auth_proof(auth_key, 'validate')
        self.auth_key = auth_key
        self.server_challenge = None
        self.session = run_id
        self.expected_seq = 1
        self.send_seq = 0
        self.challenge = None
        self.active = False
        self.application = application
        self.finished = False

    def receive(self, frame):
        message = decode(frame)
        if message['session'] != self.session:
            raise ValueError('session mismatch')
        if message['seq'] != self.expected_seq:
            raise ValueError('sequence mismatch')
        kind, body = message['kind'], message['body']
        if self.expected_seq == 1:
            if kind != 'hello' or set(body) != {'session', 'challenge', 'proof'}:
                raise ValueError('hello required before application data')
            session = check_id(body['session'])
            check_id(body['challenge'])
            expected = auth_proof(self.auth_key, 'server-hello', self.run_id, session, body['challenge'])
            if not isinstance(body['proof'], str) or not hmac.compare_digest(expected, body['proof']):
                raise ValueError('server authentication failed')
            if session == self.run_id:
                raise ValueError('connection must use a fresh session')
            self.session = session
            self.server_challenge = body['challenge']
            self.challenge = os.urandom(16).hex()
            reply_kind = 'hello_ack'
            result = {'echo': body['challenge'], 'challenge': self.challenge,
                      'pid': os.getpid(), 'python': sys.version.split()[0],
                      'proof': auth_proof(self.auth_key, 'client-hello', self.run_id,
                                          self.session, self.server_challenge, self.challenge)}
        elif not self.active:
            expected = auth_proof(self.auth_key, 'server-confirm', self.run_id,
                                  self.session, self.server_challenge, self.challenge)
            if (kind != 'confirm' or set(body) != {'echo', 'proof'} or body['echo'] != self.challenge or
                    not isinstance(body['proof'], str) or not hmac.compare_digest(expected, body['proof'])):
                raise ValueError('reverse challenge mismatch')
            self.active = True
            reply_kind, result = 'ready', {'max_frame': MAX_FRAME, 'max_queue': 8}
        elif kind == 'echo':
            samples = body.get('samples')
            if set(body) != {'samples'} or not isinstance(samples, list) or not 1 <= len(samples) <= 256:
                raise ValueError('echo batch must contain 1..256 samples')
            for sample in samples:
                if (not isinstance(sample, list) or len(sample) != 2 or
                        type(sample[0]) is not int or not 0 <= sample[0] <= 1000000 or
                        not isinstance(sample[1], str) or len(sample[1]) > 512):
                    raise ValueError('invalid synthetic sample')
            reply_kind, result = 'echo_reply', {'samples': samples}
        elif kind == 'ping':
            if body:
                raise ValueError('ping body must be empty')
            reply_kind, result = 'pong', {}
        elif kind == 'bye':
            if body:
                raise ValueError('bye body must be empty')
            self.finished = True
            reply_kind, result = 'bye_ack', {}
        elif self.application is not None:
            reply_kind, result = self.application(kind, body)
        else:
            raise ValueError('operation unavailable in qualification mode')
        self.expected_seq += 1
        self.send_seq += 1
        return encode(self.session, self.send_seq, reply_kind, result)
