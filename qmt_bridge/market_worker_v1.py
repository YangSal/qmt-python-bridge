"""Market lifecycle adapter for the separately qualified memory core."""
from .memory_client_v1 import MemoryWorker
from .market_memory_v1 import MarketApplication


class MarketMemoryWorker(MemoryWorker):
    def __init__(self, api, name, run_id, auth_key, context):
        self.market_cleanup_pending = False
        application = MarketApplication(context, max_age_ms=5000, timetag_timezone='Asia/Shanghai')
        super(MarketMemoryWorker, self).__init__(api, name, run_id, auth_key, application)

    @property
    def resources_closed(self):
        return super(MarketMemoryWorker, self).resources_closed and not self.market_cleanup_pending

    def _drop(self):
        try:
            result = self.application.close()
            self.market_cleanup_pending = result['state'] != 'complete'
        except Exception:
            self.market_cleanup_pending = True
        finally:
            super(MarketMemoryWorker, self)._drop()

    def _application(self, kind, body):
        if kind == 'market_subscribe' and self.market_cleanup_pending:
            raise ValueError('previous subscription cleanup is unresolved')
        name, result = super(MarketMemoryWorker, self)._application(kind, body)
        if kind in ('market_subscribe', 'market_unsubscribe'):
            self.market_cleanup_pending = any(
                item['state'] != 'active' for item in self.application.subscriptions.values())
        if kind == 'metrics':
            result['market_cleanup_pending'] = self.market_cleanup_pending
        return name, result
