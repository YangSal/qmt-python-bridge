"""Private versioned QMT probes and authenticated in-memory request sessions."""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import math
import os
import shutil
import struct
import time
from pathlib import Path

from qmt_bridge.memory_protocol_v1 import MAX_FRAME, auth_proof, check_id, decode, encode
from qmt_bridge.protocol import atomic_json
from .pipe_security import protect_private_directory, verify_private_file, verify_restricted_access
from .pipe_server import Server

_RETIRED = []
_RETIRED_LISTENERS = []


def _peer_closed(exc):
    # TimeoutError subclasses OSError; a local deadline is not peer closure.
    return (isinstance(exc, EOFError) or isinstance(exc, BrokenPipeError) or
            (isinstance(exc, OSError) and not isinstance(exc, TimeoutError) and
             (getattr(exc, 'winerror', None) in (109, 232, 233) or
              getattr(exc, 'errno', None) in (109, 232, 233))))


def close_listener(listener):
    if listener not in _RETIRED_LISTENERS:
        _RETIRED_LISTENERS.append(listener)
    # Keep canceled ConnectNamedPipe storage alive even if this call times out.
    listener.close()
    _RETIRED_LISTENERS.remove(listener)


def close_channel(channel, timeout=1):
    if channel is None:
        return
    _RETIRED.append(channel)
    channel.close()
    end = time.monotonic() + timeout
    while _RETIRED and time.monotonic() < end:
        for item in list(_RETIRED):
            if item.reap_close():
                _RETIRED.remove(item)
        if _RETIRED:
            time.sleep(.001)
    if _RETIRED:
        raise TimeoutError('pipe cancellation incomplete; retained for later cleanup')


def load_config(path):
    path = Path(path)
    verify_private_file(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    check_id(value['run_id'])
    auth_proof(value['auth_key'], 'validate')
    if value['pipe'] != r'\\.\pipe\qmt-memory-' + value['run_id']:
        raise ValueError('unexpected pipe name')
    return value


def prepare(directory, qualification=None, history_root=None, downloads_enabled=False):
    """Create private immutable copies BEFORE QMT imports this unique package."""
    source = Path(__file__).resolve().parents[1] / 'qmt_bridge'
    core_names = ('memory_protocol_v1.py', 'memory_io_v1.py', 'memory_client_v1.py')
    if qualification is not None:
        hashes = {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in core_names}
        if (qualification.get('status') != 'named_pipe_verified' or
                qualification.get('peer', {}).get('python') != '3.6.8' or
                qualification.get('core_sha256') != hashes):
            raise ValueError('actual accepted QMT qualification for this unchanged core required')
    directory = Path(directory).resolve()
    if type(downloads_enabled) is not bool:
        raise ValueError('downloads_enabled must be boolean')
    if history_root is not None:
        if qualification is None:
            raise ValueError('unified service requires accepted memory qualification')
        history_root = Path(history_root).resolve()
        if (history_root == directory or history_root in directory.parents or
                directory in history_root.parents):
            raise ValueError('history and private session directories must be separate')
    elif downloads_enabled:
        raise ValueError('downloads require a historical service directory')
    protect_private_directory(directory)
    run = os.urandom(16).hex()
    package = 'qmt_session_' + run
    bundle = directory / package
    bundle.mkdir()
    (bundle / '__init__.py').write_text('', encoding='ascii')
    names = core_names + (('market_memory_v1.py', 'market_worker_v1.py') if qualification is not None else ())
    if history_root is not None:
        names += ('unified_worker_v1.py', 'auto_worker.py', 'auto_protocol.py', 'worker.py', 'protocol.py')
    for name in names:
        shutil.copyfile(source / name, bundle / name)
    config = dict(schema='qmt-private-memory-session-v1', run_id=run,
                  auth_key=os.urandom(32).hex(), pipe=r'\\.\pipe\qmt-memory-' + run,
                  package=package, mode='market' if qualification is not None else 'probe',
                  strategy=str(directory / ('strategy_market.py' if qualification is not None else 'strategy_probe.py')))
    # No account/market API. All QMT scheduling uses its timer thread.
    strategy = '''# coding: ascii
import sys
import _winapi
import time
import json
SESSION_ROOT = %r
if SESSION_ROOT not in sys.path:
    sys.path.insert(0, SESSION_ROOT)
from %s.memory_client_v1 import MemoryWorker
with open(SESSION_ROOT + '/session.local.json', 'r') as _stream:
    _settings = json.load(_stream)
_worker = None
_started = None
def init(C):
    global _worker, _started
    if _worker is not None:
        _worker.close()
    _worker = MemoryWorker(_winapi, _settings['pipe'], _settings['run_id'], _settings['auth_key'])
    _started = time.monotonic()
    try:
        C.run_time('memory_poll', '50nMilliSecond', '2020-01-01 00:00:00')
    except Exception:
        _worker.close()
        raise
    print('QMT memory synthetic probe ready')
def memory_poll(C):
    if _worker is not None:
        if time.monotonic() - _started > 1800:
            _worker.close()
        _worker.poll()
def handlebar(C):
    pass
def stop(C):
    if _worker is not None:
        _worker.close()
        print('QMT memory probe stopped; resources_closed=' + str(_worker.reap()))
''' % (str(directory), package)
    if qualification is not None:
        strategy = strategy.replace('from %s.memory_client_v1 import MemoryWorker' % package,
            'from %s.market_worker_v1 import MarketMemoryWorker as MemoryWorker' % package)
        strategy = strategy.replace("_settings['auth_key'])", "_settings['auth_key'], C)")
        strategy = strategy.replace('QMT memory synthetic probe ready', 'QMT market memory v1 ready')
        strategy = strategy.replace('QMT memory probe stopped', 'QMT market memory v1 stopped')
        strategy = strategy.replace('> 1800:', '> 7200:')
        config['qualification_sha256'] = hashlib.sha256(json.dumps(
            qualification, sort_keys=True).encode('utf-8')).hexdigest()
    if history_root is not None:
        config.update(service_mode='unified', history_root=str(history_root),
                      downloads_enabled=downloads_enabled,
                      strategy=str(directory / 'strategy_collect.py'))
        strategy = strategy.replace('market_worker_v1 import MarketMemoryWorker',
                                    'unified_worker_v1 import UnifiedWorker')
        strategy = strategy.replace("_settings['auth_key'], C)",
            "_settings['auth_key'], C, _settings['history_root'], globals(), "
            "downloads_enabled=_settings['downloads_enabled'])")
        strategy = strategy.replace('memory_poll', 'collector_poll').replace('50nMilliSecond', '10nMilliSecond')
        strategy = strategy.replace('QMT market memory v1', 'QMT unified collector v1')
        strategy = strategy.replace('> 7200:', '> 86400:')
    Path(config['strategy']).write_text(strategy, encoding='ascii')
    config_path = directory / 'session.local.json'
    config_path.write_text(json.dumps(config, indent=2), encoding='utf-8')
    for file in directory.rglob('*'):
        if file.is_file():
            verify_private_file(file)
    return config_path


class Connection:
    def __init__(self, config, timeout=10, deadline=None):
        self.config = config
        self.timeout = timeout
        self.deadline = deadline if deadline is not None else math.inf
        self.channel = None
        self.listener = Server(config['pipe'])
        self.security = self.listener.security_report()
        self.session = os.urandom(16).hex()
        self.seq = 0
        self.peer = None

    def open(self):
        try:
            end = min(time.monotonic() + self.timeout, self.deadline)
            while self.channel is None:
                self.channel = self.listener.accept_nonblocking()
                if self.channel is None:
                    if time.monotonic() >= end:
                        raise TimeoutError('QMT memory peer connection timeout')
                    time.sleep(.002)
            challenge = os.urandom(16).hex()
            key, run = self.config['auth_key'], self.config['run_id']
            self.seq = 1
            self.channel.send(encode(run, 1, 'hello', dict(session=self.session, challenge=challenge,
                proof=auth_proof(key, 'server-hello', run, self.session, challenge))))
            hello = self._receive('hello_ack')
            check_id(hello['challenge'])
            expected = auth_proof(key, 'client-hello', run, self.session, challenge, hello['challenge'])
            if (hello['echo'] != challenge or not isinstance(hello['proof'], str) or
                    not hmac.compare_digest(expected, hello['proof'])):
                raise ValueError('QMT peer authentication failed')
            self.peer = {k: hello[k] for k in ('pid', 'python')}
            ready = self.call('confirm', dict(echo=hello['challenge'], proof=auth_proof(
                key, 'server-confirm', run, self.session, challenge, hello['challenge'])), 'ready')
            if ready != dict(max_frame=262144, max_queue=8):
                raise ValueError('unexpected transport bounds')
            return self
        except BaseException:
            self.close()
            raise

    def _receive(self, expected_kind):
        end = min(time.monotonic() + self.timeout, self.deadline)
        while time.monotonic() < end:
            frames = self.channel.poll()
            if frames:
                if len(frames) != 1:
                    raise ValueError('unexpected unsolicited reply')
                message = decode(frames[0])
                if (message['session'] != self.session or message['seq'] != self.seq or
                        message['kind'] != expected_kind):
                    raise ValueError('reply identity/sequence/kind mismatch')
                return message['body']
            time.sleep(.001)
        raise TimeoutError('QMT memory response timeout; request outcome unknown')

    def call(self, kind, body=None, expected_kind=None):
        self.seq += 1
        self.channel.send(encode(self.session, self.seq, kind, body or {}))
        return self._receive(expected_kind or kind + '_reply')

    def close(self):
        try:
            if self.channel is not None:
                channel, self.channel = self.channel, None
                close_channel(channel)
        finally:
            close_listener(self.listener)


def distribution(values):
    values = sorted(values)
    if not values:
        raise ValueError('no timing samples')
    return dict(count=len(values), p50=values[math.ceil(len(values)*.5)-1],
                p95=values[math.ceil(len(values)*.95)-1],
                p99=values[math.ceil(len(values)*.99)-1], max=values[-1])


def _literal_frame(value):
    raw = json.dumps(value, separators=(',', ':')).encode('ascii')
    return b'QMP1' + struct.pack('!I', len(raw)) + hashlib.sha256(raw).digest() + raw


def qualify(config_path, timeout=15, idle_samples=20, max_seconds=300):
    """Synthetic gates only; an independent manual restart remains mandatory."""
    if not 0 < timeout <= 30 or not 0 < max_seconds <= 300 or not 1 <= idle_samples <= 100:
        raise ValueError('bounded positive probe limits required')
    report = dict(schema='qmt-memory-qualification-v1', status='incomplete', gates={}, stage='configuration')
    try:
        _qualify(config_path, timeout, idle_samples, time.monotonic()+max_seconds, report)
    except Exception as exc:
        report['failure'] = dict(stage=report['stage'], error=type(exc).__name__)
    return report


def _qualify(config_path, timeout, idle_samples, deadline, report):
    config = load_config(config_path)
    report['run_id'] = config['run_id']
    bundle = Path(config_path).parent / config['package']
    report['core_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in bundle.glob('memory_*.py')}
    report['stage'] = 'isolation'
    name = r'\\.\pipe\qmt-memory-isolation-' + os.urandom(16).hex()
    isolation = Server(name)
    try:
        denied = verify_restricted_access(name)
        report['gates']['isolation'] = dict(status='passed', method='restricted-token separate process',
            scope='same Server policy; not a separate logged-in account', **denied)
    finally:
        close_listener(isolation)
    rtt = []
    report['stage'] = 'connection'
    connection = Connection(config, timeout, deadline).open()
    try:
        report['peer'] = connection.peer
        report['gates']['transport'] = dict(status='passed', **connection.security)
        report['gates']['authentication'] = dict(status='passed', method='HMAC-SHA256 mutual challenge')
        metrics = connection.call('metrics', {'reset_intervals': True})
        report['instance'] = metrics['instance']
        report['stage'] = 'synthetic'
        verified = 0
        for start in range(0, 10000, 100):
            samples = [[i, hashlib.sha256(('synthetic-%d' % i).encode()).hexdigest()]
                       for i in range(start, min(start + 100, 10000))]
            before = time.monotonic()
            returned = connection.call('echo', {'samples': samples})
            rtt.append((time.monotonic() - before) * 1000)
            if returned != {'samples': samples}:
                raise ValueError('synthetic sample mismatch')
            verified += len(samples)
        report['gates']['synthetic'] = dict(status='passed', verified_samples=verified,
                                          roundtrip_ms=distribution(rtt))
        load_metrics = connection.call('metrics', {'reset_intervals': True})
        report['stage'] = 'boundary'
        padding = MAX_FRAME - len(encode(connection.session, connection.seq + 1, 'boundary', {'padding': ''}))
        body = {'padding': 'x' * padding}
        if len(encode(connection.session, connection.seq + 1, 'boundary', body)) != 262144:
            raise AssertionError('boundary frame is not exactly 256 KiB')
        result = connection.call('boundary', body)
        if result != dict(bytes=padding, sha256=hashlib.sha256(body['padding'].encode()).hexdigest()):
            raise ValueError('boundary verification failed')
        report['gates']['boundary'] = dict(status='passed', bytes=262144)
        report['stage'] = 'queue'
        queue = connection.call('queue_test')
        if queue != dict(accepted=8, overflow_rejected=True, remaining=0):
            raise ValueError('queue overflow not rejected')
        report['gates']['queue'] = dict(status='passed', **queue)
        report['stage'] = 'scheduling'
        connection.call('metrics', {'reset_intervals': True})
        idle = []
        for _ in range(idle_samples):
            time.sleep(.05)
            before = time.monotonic()
            connection.call('ping', expected_kind='pong')
            idle.append((time.monotonic() - before)*1000)
        metrics = connection.call('metrics')
        if metrics['qmt_calls'] != 0 or metrics['retired_channels']:
            raise ValueError('synthetic phase called QMT or leaked retired I/O')
        report['gates']['scheduling'] = dict(status='passed', qmt_calls=0,
            idle_roundtrip_ms=distribution(idle), idle_interval_ms=distribution(metrics['intervals_ms']),
            load_interval_ms=distribution(load_metrics['intervals_ms']),
            idle_callback_max_ms=metrics['poll_max_ms'], load_callback_max_ms=load_metrics['poll_max_ms'])
        report['gates']['restart'] = dict(status='incomplete', reason='user-controlled stop/restart not yet tested')
        report['previous_session'] = connection.session
    finally:
        connection.close()
    passed = []
    for fault in ('checksum', 'truncated', 'protocol', 'old_session', 'duplicate_seq', 'oversized'):
        report['stage'] = 'fault_' + fault
        connection = Connection(config, timeout, deadline).open()
        try:
            frame = encode(connection.session, connection.seq+1, 'ping', {})
            if fault == 'checksum':
                frame = frame[:8] + b'0'*32 + frame[40:]
            elif fault == 'truncated':
                frame = frame[:-1]
            elif fault == 'protocol':
                value = json.loads(frame[40:]); value['protocol'] = 'foreign-v2'
                frame = _literal_frame(value)
            elif fault == 'old_session':
                frame = encode(report['previous_session'], connection.seq+1, 'ping', {})
            elif fault == 'duplicate_seq':
                frame = encode(connection.session, connection.seq, 'ping', {})
            else:
                frame = b'x' * (262144+1)
                connection.channel.max_bytes = len(frame)  # Fault injector only.
            connection.channel.send(frame)
            until = min(time.monotonic()+timeout, deadline)
            disconnected = False
            while time.monotonic()<until:
                try:
                    if connection.channel.poll():
                        raise ValueError('malformed request received a reply')
                except (OSError, EOFError) as exc:
                    if not _peer_closed(exc):
                        raise
                    disconnected = True
                    break
                time.sleep(.002)
            if not disconnected:
                raise TimeoutError('peer did not reject ' + fault)
            passed.append(fault)
        finally:
            connection.close()
    report['gates']['faults'] = dict(status='passed', passed=passed)
    report['stage'] = 'cleanup'
    connection = Connection(config, timeout, deadline).open()
    try:
        metrics = connection.call('metrics')
        if metrics['rejections'] < len(passed) or metrics['retired_channels']:
            raise ValueError('rejection/cleanup evidence missing')
        report['gates']['cleanup'] = dict(status='passed', retired_channels=0,
                                        rejections=metrics['rejections'])
        connection.call('bye', expected_kind='bye_ack')
    finally:
        connection.close()
    report['stage'] = 'restart_pending'


def restart_check(config_path, baseline, user_confirmed_resources_closed, timeout=15):
    """Finish the separate user-controlled restart gate; no QMT UI automation."""
    report = copy.deepcopy(baseline)
    report.update(status='incomplete', stage='restart_prerequisites')
    report.pop('failure', None)
    try:
        required = {'transport', 'authentication', 'synthetic', 'boundary', 'queue',
                    'scheduling', 'faults', 'cleanup', 'isolation', 'disconnect'}
        gates = report.get('gates', {})
        if (user_confirmed_resources_closed is not True or
                any(gates.get(name, {}).get('status') != 'passed' for name in required) or
                gates['synthetic'].get('verified_samples', 0) < 10000 or
                gates['disconnect'].get('observer_version') != 2):
            raise ValueError('baseline or user-confirmed stopped resource cleanup missing')
        config = load_config(config_path)
        if config['run_id'] != report['run_id']:
            raise ValueError('baseline belongs to another probe')
        report['stage'] = 'restart_connection'
        deadline = time.monotonic() + 30
        connection = Connection(config, min(timeout, 15), deadline).open()
        try:
            metrics = connection.call('metrics')
            if metrics['instance'] == report['instance']:
                raise ValueError('independent probe was not restarted')
            if metrics['qmt_calls'] or metrics['retired_channels']:
                raise ValueError('new probe has unexpected market calls or pending resources')
            if connection.peer['python'] != '3.6.8':
                raise ValueError('target embedded Python 3.6.8 not observed')
            # A new connection must reject the session used before the strategy restart.
            connection.channel.send(encode(report['previous_session'], connection.seq+1, 'ping', {}))
            end = min(time.monotonic()+timeout, deadline)
            rejected = False
            while time.monotonic() < end:
                try:
                    if connection.channel.poll():
                        raise ValueError('stale session accepted after restart')
                except (OSError, EOFError) as exc:
                    if not _peer_closed(exc):
                        raise
                    rejected = True
                    break
                time.sleep(.002)
            if not rejected:
                raise TimeoutError('old session rejection not observed')
        finally:
            connection.close()
        connection = Connection(config, min(timeout, 15), deadline).open()
        try:
            recovered = connection.call('metrics')
            if recovered['instance'] != metrics['instance'] or recovered['retired_channels']:
                raise ValueError('reconnect resource/instance mismatch')
            connection.call('ping', expected_kind='pong')
        finally:
            connection.close()
        report['gates']['restart'] = dict(status='passed', instance_changed=True,
            user_confirmed_resources_closed=True, old_session_rejected=True, fresh_session_recovered=True)
        report.update(status='named_pipe_verified', stage='complete')
    except Exception as exc:
        report['failure'] = dict(stage=report['stage'], error=type(exc).__name__)
    return report


def watch_disconnect(config_path, baseline, max_seconds=180):
    """Keep a request outstanding while the USER stops the independent probe."""
    report = copy.deepcopy(baseline)
    report.update(status='incomplete', stage='disconnect_watch')
    try:
        if not 0 < max_seconds <= 300 or baseline.get('stage') != 'restart_pending' or 'failure' in baseline:
            raise ValueError('complete baseline and bounded watch required')
        config = load_config(config_path)
        connection = Connection(config, 15, time.monotonic()+max_seconds).open()
        try:
            instance = connection.call('metrics')['instance']
            if instance != baseline['instance']:
                raise ValueError('probe changed before controlled stop observation')
            while True:
                try:
                    connection.call('ping', expected_kind='pong')
                except (OSError, EOFError) as exc:
                    if not _peer_closed(exc):
                        raise
                    report['gates']['disconnect'] = dict(status='passed',
                        observer_version=2, pending_request=True, peer_closed=True, stale_cache_delivered=False)
                    report['stage'] = 'restart_pending'
                    break
        finally:
            connection.close()
    except Exception as exc:
        report['failure'] = dict(stage=report['stage'], error=type(exc).__name__)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--directory', required=True)
    p.add_argument('--qualification', help='accepted terminal report; prepares market mode')
    p.add_argument('--history-dir', help='separate historical runtime; prepares one unified QMT entry')
    p.add_argument('--enable-downloads', action='store_true', help='opt in to native historical downloads')
    p = sub.add_parser('qualify'); p.add_argument('--config', required=True)
    p.add_argument('--report', required=True); p.add_argument('--timeout', type=float, default=15)
    p = sub.add_parser('restart'); p.add_argument('--config', required=True)
    p.add_argument('--baseline', required=True); p.add_argument('--report', required=True)
    p.add_argument('--resources-closed-confirmed', action='store_true')
    p = sub.add_parser('watch-stop'); p.add_argument('--config', required=True)
    p.add_argument('--baseline', required=True); p.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        qualification = json.loads(Path(args.qualification).read_text()) if args.qualification else None
        print(prepare(args.directory, qualification=qualification, history_root=args.history_dir,
                      downloads_enabled=args.enable_downloads))
        return 0
    elif args.command == 'qualify':
        result = qualify(args.config, timeout=args.timeout)
    elif args.command == 'watch-stop':
        baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        result = watch_disconnect(args.config, baseline)
    else:
        baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        result = restart_check(args.config, baseline, args.resources_closed_confirmed)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(Path(args.report), result)
    print(json.dumps(dict(status=result['status'], stage=result['stage'], failure=result.get('failure'))))
    return 1 if result.get('failure') else 0


if __name__ == '__main__':
    raise SystemExit(main())
