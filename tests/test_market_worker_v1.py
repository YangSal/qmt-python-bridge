import ast
import importlib.util
from pathlib import Path

import pytest


def test_disconnect_and_stop_retire_only_owned_subscriptions():
    assert importlib.util.find_spec('qmt_bridge.market_worker_v1'), 'market lifecycle integration missing'
    from qmt_bridge.market_worker_v1 import MarketMemoryWorker
    class Context:
        canceled = []
        def subscribe_quote(self, **kwargs):
            return 17
        def unsubscribe_quote(self, ident):
            self.canceled.append(ident)
    context = Context()
    worker = MarketMemoryWorker(None, r'\\.\pipe\qmt-memory-test', 'a'*32, 'b'*64, context)
    worker.application('market_subscribe', {'codes': ['000001.SZ']})
    generation = worker.application.generation
    worker._drop()
    assert context.canceled == [17]
    assert worker.application.generation != generation
    assert worker.resources_closed
    worker.close()
    assert context.canceled == [17]


def test_cleanup_failure_is_visible_and_not_lost():
    from qmt_bridge.market_worker_v1 import MarketMemoryWorker
    class Context:
        def subscribe_quote(self, **kwargs):
            return 19
        def unsubscribe_quote(self, ident):
            raise RuntimeError('private native detail')
    worker = MarketMemoryWorker(None, r'\\.\pipe\qmt-memory-test', 'a'*32, 'b'*64, Context())
    worker.application('market_subscribe', {'codes': ['000001.SZ']})
    worker.close()
    assert not worker.resources_closed
    assert worker._application('metrics', {})[1]['market_cleanup_pending'] is True
    assert worker.application.subscriptions['000001.SZ']['subscription_id'] == 19
    ast.parse(Path(__import__('qmt_bridge.market_worker_v1', fromlist=['x']).__file__).read_text(), feature_version=(3, 6))


def test_explicit_cleanup_resolves_health_before_accepting_new_subscriptions():
    from qmt_bridge.market_worker_v1 import MarketMemoryWorker

    class Context:
        failed = True
        calls = 0
        def subscribe_quote(self, **kwargs):
            self.calls += 1
            return self.calls
        def unsubscribe_quote(self, ident):
            if self.failed:
                raise RuntimeError('native cleanup failure')

    context = Context()
    worker = MarketMemoryWorker(None, r'\\.\pipe\qmt-memory-test', 'a'*32, 'b'*64, context)
    worker._application('market_subscribe', {'codes': ['000001.SZ']})
    worker._drop()  # A failed cleanup is retained across a disconnected session.
    assert worker.market_cleanup_pending
    with pytest.raises(ValueError, match='cleanup'):
        worker._application('market_subscribe', {'codes': ['510300.SH']})
    assert context.calls == 1
    context.failed = False
    result = worker._application('market_unsubscribe', {'codes': ['000001.SZ']})[1]
    assert result['state'] == 'complete'
    assert worker._application('metrics', {})[1]['market_cleanup_pending'] is False
    assert worker.resources_closed
    worker._application('market_subscribe', {'codes': ['510300.SH']})
    assert context.calls == 2
    worker.close()
