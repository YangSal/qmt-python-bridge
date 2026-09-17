import ast
import hashlib
import hmac
import importlib.util
import json
import struct
from pathlib import Path

import pytest

KEY = 'd' * 64


def proof(role, *parts, key=KEY):
    return hmac.new(bytes.fromhex(key), ('|'.join((role,) + parts)).encode(), hashlib.sha256).hexdigest()


def hello():
    return {'challenge': 'b'*32, 'session': 'c'*32,
            'proof': proof('server-hello', 'a'*32, 'c'*32, 'b'*32)}


def confirm(challenge):
    return {'echo': challenge, 'proof': proof('server-confirm', 'a'*32, 'c'*32, 'b'*32, challenge)}


def module():
    assert importlib.util.find_spec('qmt_bridge.memory_protocol_v1'), 'memory protocol not implemented'
    from qmt_bridge import memory_protocol_v1
    return memory_protocol_v1


def packet(session='a' * 32, seq=1, kind='ping', body=None, protocol='qmt-memory-v1'):
    raw = json.dumps({'protocol': protocol, 'session': session, 'seq': seq,
                      'kind': kind, 'body': body or {}}, separators=(',', ':')).encode()
    return b'QMP1' + struct.pack('!I', len(raw)) + hashlib.sha256(raw).digest() + raw


def test_wire_contract_and_independent_literal_reader():
    m = module()
    encoded = m.encode('a' * 32, 7, 'echo', {'samples': [[1, 'test']]})
    assert encoded[:4] == b'QMP1'
    assert int.from_bytes(encoded[4:8], 'big') == len(encoded) - 40
    assert encoded[8:40] == hashlib.sha256(encoded[40:]).digest()
    body = json.loads(encoded[40:])
    assert body == {'protocol': 'qmt-memory-v1', 'session': 'a' * 32,
                    'seq': 7, 'kind': 'echo', 'body': {'samples': [[1, 'test']]}}
    assert m.decode(packet())['kind'] == 'ping'


@pytest.mark.parametrize('raw', [b'', b'QMP1' + b'\0' * 39,
    packet()[:-1], packet() + b'x', packet(protocol='bad'), packet(session='wrong'),
    packet(seq=True), packet(seq=0), packet()[:40] + b'x' * (len(packet()) - 40),
    b'QMP1' + struct.pack('!I', 300000) + b'x' * 32])
def test_corrupt_length_version_or_identity_is_rejected(raw):
    with pytest.raises(ValueError):
        module().decode(raw)


def test_rejects_json_nonfinite_and_duplicate_keys():
    m = module()
    with pytest.raises(ValueError):
        m.encode('a' * 32, 1, 'ping', {'x': float('nan')})
    raw = b'{"protocol":"qmt-memory-v1","protocol":"bad"}'
    with pytest.raises(ValueError):
        m.decode(b'QMP1' + struct.pack('!I', len(raw)) + hashlib.sha256(raw).digest() + raw)


def test_embedded_handshake_and_session_replay_rejection():
    m = module()
    peer = m.EmbeddedSession('a' * 32, KEY)
    with pytest.raises(ValueError):
        peer.receive(packet(kind='echo', body={'samples': [[1, 'a']]}))
    ack = m.decode(peer.receive(packet(kind='hello', body=hello())))
    assert ack['kind'] == 'hello_ack' and ack['session'] == 'c'*32
    assert ack['body']['echo'] == 'b'*32
    assert ack['body']['proof'] == proof('client-hello','a'*32,'c'*32,'b'*32,ack['body']['challenge'])
    ready = m.decode(peer.receive(packet('c'*32, 2, 'confirm', confirm(ack['body']['challenge']))))
    assert ready['kind'] == 'ready'
    reply = m.decode(peer.receive(packet('c'*32, 3, 'echo', {'samples': [[0, 'zero'], [1, 'one']]})))
    assert reply['body']['samples'] == [[0, 'zero'], [1, 'one']]
    with pytest.raises(ValueError, match='sequence'):
        peer.receive(packet('c'*32, 3, 'ping'))
    with pytest.raises(ValueError, match='session'):
        peer.receive(packet('a'*32, 4, 'ping'))


def test_handshake_wrong_reverse_challenge_never_activates():
    m = module()
    peer = m.EmbeddedSession('a'*32, KEY)
    peer.receive(packet(kind='hello', body=hello()))
    with pytest.raises(ValueError):
        peer.receive(packet('c'*32, 2, 'confirm', {'echo': 'f'*32}))
    assert peer.active is False


def test_echo_batch_is_bounded_and_never_calls_qmt():
    m = module()
    peer = m.EmbeddedSession('a'*32, KEY)
    ack = m.decode(peer.receive(packet(kind='hello', body=hello())))
    peer.receive(packet('c'*32,2,'confirm',confirm(ack['body']['challenge'])))
    with pytest.raises(ValueError):
        peer.receive(packet('c'*32,3,'echo',{'samples':[[i,'x'] for i in range(257)]}))


def test_embedded_module_is_python36_syntax():
    m = module()
    ast.parse(Path(m.__file__).read_text(), feature_version=(3,6))


def test_rogue_server_without_key_cannot_complete_first_handshake():
    peer = module().EmbeddedSession('a'*32, KEY)
    body = hello()
    body['proof'] = proof('server-hello', 'a'*32, 'c'*32, 'b'*32, key='e'*64)
    with pytest.raises(ValueError, match='authentication'):
        peer.receive(packet(kind='hello', body=body))
    assert peer.active is False and peer.expected_seq == 1


def test_auth_key_required_and_never_serialized():
    with pytest.raises(ValueError):
        module().EmbeddedSession('a'*32, '')
    reply = module().EmbeddedSession('a'*32, KEY).receive(packet(kind='hello', body=hello()))
    assert KEY.encode() not in reply


@pytest.mark.parametrize('number',['1e999','-1e999','1'*200])
def test_json_overflow_and_unbounded_integers_rejected(number):
    raw = ('{"protocol":"qmt-memory-v1","session":"'+'a'*32+
           '","seq":1,"kind":"ping","body":{"x":'+number+'}}').encode()
    with pytest.raises(ValueError):
        module().decode(b'QMP1'+struct.pack('!I',len(raw))+hashlib.sha256(raw).digest()+raw)
