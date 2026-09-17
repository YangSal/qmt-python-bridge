"""One timer owns historical requests and authenticated market snapshots."""
import time

from .auto_worker import AutomaticWorker
from .market_worker_v1 import MarketMemoryWorker


class UnifiedWorker(MarketMemoryWorker):
    def __init__(self, api, name, run_id, auth_key, context, history_root, namespace,
                 downloads_enabled=False):
        self.history = None
        self._service_polling = False
        self.history_suspended = False
        self.history_error = None
        self.history_failures = 0
        self.history_processed = 0
        self.history_overruns = 0
        self.service_max_ms = 0.
        super(UnifiedWorker, self).__init__(api, name, run_id, auth_key, context)
        try:
            self.history = AutomaticWorker(history_root, context, namespace,
                                           downloads_enabled=downloads_enabled)
        except Exception:
            self.close()
            raise

    @property
    def resources_closed(self):
        return self.history is None and super(UnifiedWorker, self).resources_closed

    def _application(self, kind, body):
        if kind == 'service_status' and body == {}:
            return 'service_status_reply', {
                'mode': 'unified_history_and_market', 'trading_ready': False,
                'history_suspended': self.history_suspended, 'history_error': self.history_error,
                'history_processed': self.history_processed, 'history_overruns': self.history_overruns,
                'service_max_ms': self.service_max_ms,
                'history_enabled': self.history is not None,
                'market_cleanup_pending': self.market_cleanup_pending}
        return super(UnifiedWorker, self)._application(kind, body)

    def poll(self):
        if self._service_polling:
            return
        if self.stopped:
            self.reap()
            return
        self._service_polling = True
        started = time.perf_counter()
        try:
            super(UnifiedWorker, self).poll()
            if not self.history_suspended and self.history is not None:
                for unused in range(8):
                    if time.perf_counter() - started >= .020:
                        break
                    try:
                        processed = self.history.poll()
                        self.history_failures = 0
                        self.history_error = None
                    except Exception as exc:
                        self.history_failures += 1
                        self.history_error = type(exc).__name__
                        if self.history_failures >= 3:
                            self.history_suspended = True
                        break
                    self.history_processed += int(bool(processed))
                    # A synchronous QMT call can overrun; progress memory directly
                    # afterward instead of starting a second native request first.
                    super(UnifiedWorker, self).poll()
                    if not processed:
                        break
        finally:
            elapsed = time.perf_counter() - started
            self.history_overruns += int(elapsed > .020)
            self.service_max_ms = max(self.service_max_ms, elapsed * 1000)
            self._service_polling = False

    def close(self):
        try:
            if self.history is not None:
                self.history.close()
                self.history = None
        finally:
            super(UnifiedWorker, self).close()
