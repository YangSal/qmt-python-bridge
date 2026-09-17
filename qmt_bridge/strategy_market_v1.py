# coding: ascii
"""Isolated historical worker scheduler; its timer rate requires terminal proof."""
# This is a NEW entry point. Do not replace or hot-reload a running strategy.
# A native QMT call remains synchronous: the callback budget prevents starting
# more work after an overrun, but cannot interrupt an in-flight native call.
import sys
import time

PROJECT_ROOT = r'D:\bigqmt-data-bridge'
BRIDGE_DIR = r'D:\bigqmt-market-runtime'
ENABLE_DOWNLOADS = False
MAX_POLLS = 8
CALLBACK_BUDGET_SECONDS = 0.020

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from qmt_bridge.auto_worker import AutomaticWorker

_worker = None
_polling = False
_metrics = {'callbacks': 0, 'processed': 0, 'over_budget_callbacks': 0,
            'max_callback_seconds': 0.0}


def init(C):
    global _worker, _polling, _metrics
    if _worker is not None:
        _worker.close()
        _worker = None
    _polling = False
    _metrics = {'callbacks': 0, 'processed': 0, 'over_budget_callbacks': 0,
                'max_callback_seconds': 0.0}
    _worker = AutomaticWorker(BRIDGE_DIR, C, globals(),
                              downloads_enabled=ENABLE_DOWNLOADS)
    try:
        C.run_time('bridge_market_poll', '10nMilliSecond', '2020-01-01 00:00:00')
    except Exception:
        _worker.close()
        _worker = None
        raise
    print('QMT market history v1 ready: ' + BRIDGE_DIR +
          '; requested_timer=10nMilliSecond; max_polls=8; soft_budget_ms=20; downloads=' +
          str(ENABLE_DOWNLOADS))


def bridge_market_poll(C):
    global _polling
    if _worker is None or _polling:
        return
    _polling = True
    started = time.perf_counter()
    processed = 0
    try:
        while processed < MAX_POLLS and time.perf_counter() - started < CALLBACK_BUDGET_SECONDS:
            if not _worker.poll():
                break
            processed += 1
    finally:
        elapsed = time.perf_counter() - started
        _metrics['callbacks'] += 1
        _metrics['processed'] += processed
        _metrics['max_callback_seconds'] = max(_metrics['max_callback_seconds'], elapsed)
        if elapsed > CALLBACK_BUDGET_SECONDS:
            _metrics['over_budget_callbacks'] += 1
        _polling = False


def handlebar(C):
    pass


def stop(C):
    global _worker
    if _worker is not None:
        _worker.close()
        _worker = None
    print('QMT market history v1 stopped; scheduling counters: ' + str(_metrics))
