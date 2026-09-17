"""Restricted-Python memory worker. QMT timer owns all API calls and pipe work."""
import hashlib
import os
import time
from collections import deque

from .memory_io_v1 import connect
from .memory_protocol_v1 import EmbeddedSession, MAX_FRAME

# Closing OVERLAPPED storage must survive even when a strategy stops/reinitializes.
# This list owns only this versioned module's retired channels, never other workers.
_RETIRED = []


def reap_retired():
    for channel in list(_RETIRED):
        if channel.reap_close():
            _RETIRED.remove(channel)


class MemoryWorker(object):
    def __init__(self, api, name, run_id, auth_key, application=None):
        self.api, self.name, self.run_id, self.auth_key = api, name, run_id, auth_key
        self.application = application
        self.channel = None
        self.session = None
        self.instance = os.urandom(16).hex()
        self.connections = 0
        self.rejections = 0
        self.last_error = None
        self.last_tick = None
        self.intervals = deque(maxlen=2000)
        self.poll_max_ms = 0.
        self.poll_count = 0
        self.stopped = False
        self.next_connect = 0.
        self.qmt_calls = 0

    @property
    def resources_closed(self):
        return self.channel is None and not _RETIRED

    def reap(self):
        reap_retired()
        return self.resources_closed

    def _drop(self):
        if self.channel is not None:
            channel, self.channel = self.channel, None
            # Transfer ownership BEFORE attempting cancel, including exceptions.
            _RETIRED.append(channel)
            channel.close()
            reap_retired()
        self.session = None

    def close(self):
        self.stopped = True
        self._drop()

    def _application(self, kind, body):
        if kind == 'metrics' and (not body or body == {'reset_intervals': True}):
            result = {'instance': self.instance, 'connections': self.connections,
                'rejections': self.rejections, 'poll_count': self.poll_count,
                'poll_max_ms': self.poll_max_ms, 'intervals_ms': list(self.intervals),
                'qmt_calls': self.qmt_calls, 'retired_channels': len(_RETIRED)}
            if body:
                self.intervals.clear()
                self.poll_max_ms = 0.
            return 'metrics_reply', result
        if kind == 'boundary' and set(body) == {'padding'} and isinstance(body['padding'], str):
            value = body['padding'].encode('ascii')
            return 'boundary_reply', {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
        if kind == 'queue_test' and not body:
            if self.channel.pending_writes:
                raise ValueError('queue test requires an empty outbound queue')
            rejected = False
            try:
                for unused in range(8):
                    self.channel.send(b'synthetic-queue-probe')
                try:
                    self.channel.send(b'overflow')
                except BufferError:
                    rejected = True
                accepted = self.channel.pending_writes
            finally:
                self.channel.queue.clear()  # No native write runs during this probe.
            return 'queue_test_reply', {'accepted': accepted, 'overflow_rejected': rejected,
                                        'remaining': self.channel.pending_writes}
        if self.application is not None:
            self.qmt_calls += 1
            return self.application(kind, body)
        raise ValueError('operation unavailable in synthetic qualification')

    def poll(self):
        started = time.monotonic()
        reap_retired()
        if self.stopped:
            return
        if self.last_tick is not None:
            self.intervals.append((started - self.last_tick) * 1000.)
        self.last_tick = started
        self.poll_count += 1
        try:
            if self.channel is None:
                if started < self.next_connect or _RETIRED:
                    return
                self.next_connect = started + .1
                try:
                    self.channel = connect(self.api, self.name)
                except OSError as exc:
                    self.last_error = 'connect_%s' % getattr(exc, 'winerror', 'error')
                    return
                self.session = EmbeddedSession(self.run_id, self.auth_key, self._application)
                self.connections += 1
            for frame in self.channel.poll(limit=4):
                reply = self.session.receive(frame)
                self.channel.send(reply)
            # Progress replies without discarding a request received concurrently.
            for frame in self.channel.poll(limit=1):
                self.channel.send(self.session.receive(frame))
        except Exception as exc:
            self.rejections += 1
            self.last_error = type(exc).__name__  # Never copy frame or market data into logs.
            self._drop()
        finally:
            self.poll_max_ms = max(self.poll_max_ms, (time.monotonic() - started) * 1000.)
