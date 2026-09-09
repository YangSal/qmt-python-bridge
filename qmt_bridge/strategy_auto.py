# coding: ascii
"""Explicit opt-in QMT strategy for the durable automatic K-line worker."""
# Imported worker modules stay cached. Once imported, upgrades require a safely
# arranged QMT client restart or a separately versioned module; rerunning this
# same strategy cannot refresh them. No hot reload is implemented.
import sys

PROJECT_ROOT = r'D:\bigqmt-data-bridge'
BRIDGE_DIR = r'D:\bigqmt-auto-runtime'
ENABLE_DOWNLOADS = False
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from qmt_bridge.auto_worker import AutomaticWorker

_worker = None


def init(C):
    global _worker
    if _worker is not None:
        _worker.close()
        _worker = None
    _worker = AutomaticWorker(BRIDGE_DIR, C, globals(),
                              downloads_enabled=ENABLE_DOWNLOADS)
    C.run_time('bridge_poll', '1nSecond', '2020-01-01 00:00:00')
    print('QMT automatic K-line bridge ready: ' + BRIDGE_DIR)


def bridge_poll(C):
    if _worker is not None:
        _worker.poll()


def handlebar(C):
    pass


def stop(C):
    global _worker
    if _worker is not None:
        _worker.close()
        _worker = None
