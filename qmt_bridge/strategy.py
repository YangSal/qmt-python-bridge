# coding: ascii
"""Load this ASCII file in the QMT strategy editor, standard model mode."""
import sys
import importlib

PROJECT_ROOT = r'D:\bigqmt-data-bridge'
BRIDGE_DIR = r'D:\bigqmt-data-bridge-runtime'
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_worker = None


def init(C):
    global _worker
    if _worker is not None:
        _worker.close()
        _worker = None
    from qmt_bridge import worker
    importlib.reload(worker)
    _worker = worker.Worker(BRIDGE_DIR, C, globals())
    C.run_time('bridge_poll', '2nSecond', '2020-01-01 00:00:00')
    print('QMT data bridge ready: ' + BRIDGE_DIR)


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
