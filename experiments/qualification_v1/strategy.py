# coding: ascii
"""Throwaway qualification entry. No orders, no native xtquant fallback."""
import sys

PROJECT_ROOT = r'D:\bigqmt-data-bridge'
RUNTIME_ROOT = r'D:\bigqmt-qualification-runtime'
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_qualification = None


def init(C):
    global _qualification
    from experiments.qualification_v1.worker import QualificationWorker
    if _qualification is not None:
        _qualification.close()
    _qualification = QualificationWorker(RUNTIME_ROOT, C, globals())
    C.run_time('qualification_poll', '1nSecond', '2020-01-01 00:00:00')
    print('QMT qualification v1 ready; trading disabled: ' + RUNTIME_ROOT)


def qualification_poll(C):
    if _qualification is not None:
        _qualification.poll()


def handlebar(C):
    pass


def stop(C):
    global _qualification
    if _qualification is not None:
        _qualification.close()
        _qualification = None
