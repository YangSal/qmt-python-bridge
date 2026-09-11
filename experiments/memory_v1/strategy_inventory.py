import datetime
import json
import os
import platform


PROTOCOL = "memory-inventory-v1"
SESSION_ID = ""
REPORT_DIR = r"D:\bigqmt-data-bridge\evidence\memory-v1"
MODULE_CALLABLES = (
    ("mmap", ("mmap",)),
    ("_winapi", (
        "CreateNamedPipe", "ConnectNamedPipe", "CreateFile", "ReadFile",
        "WriteFile", "PeekNamedPipe", "SetNamedPipeHandleState", "CloseHandle",
        "WaitForSingleObject", "CreateMutex", "ReleaseMutex",
    )),
    ("_multiprocessing", ("SemLock",)),
    ("ctypes", ("WinDLL",)),
    ("socket", ("socket",)),
    ("msvcrt", ("locking",)),
)
QMT_CALLABLE_NAMES = (
    "run_time", "schedule_run", "subscribe_quote", "subscribe_whole_quote",
    "get_full_tick", "get_trade_detail_data", "passorder", "cancel",
)
STAGE_NAMES = (
    "cross_process", "synchronization", "security", "load", "recovery",
    "scheduling",
)
MAX_ERROR_LENGTH = 240


def _validate_session_id(session_id):
    if not isinstance(session_id, str) or len(session_id) != 32:
        raise ValueError("session_id must be 32 lowercase hexadecimal characters")
    if any(character not in "0123456789abcdef" for character in session_id):
        raise ValueError("session_id must be 32 lowercase hexadecimal characters")


def _error_text(error):
    text = "%s: %s" % (error.__class__.__name__, str(error))
    return text[:MAX_ERROR_LENGTH]


def _callable_attribute(target, name):
    try:
        return callable(getattr(target, name)), None
    except Exception as error:
        return False, _error_text(error)


def _collect_modules():
    result = {}
    for module_name, names in MODULE_CALLABLES:
        callable_results = dict((name, False) for name in names)
        errors = []
        try:
            module = __import__(module_name)
        except Exception as error:
            result[module_name] = {
                "importable": False,
                "error": _error_text(error),
                "callables": callable_results,
            }
            continue
        for name in names:
            present, error_text = _callable_attribute(module, name)
            callable_results[name] = present
            if error_text is not None:
                errors.append("%s: %s" % (name, error_text))
        result[module_name] = {
            "importable": True,
            "error": "; ".join(errors)[:MAX_ERROR_LENGTH] if errors else None,
            "callables": callable_results,
        }
    return result


def _collect_qmt_side(target, mapping):
    callables = {}
    errors = {}
    for name in QMT_CALLABLE_NAMES:
        try:
            value = target[name] if mapping else getattr(target, name)
            callables[name] = callable(value)
        except (KeyError, AttributeError):
            callables[name] = False
        except Exception as error:
            callables[name] = False
            errors[name] = _error_text(error)
    return callables, errors


def collect_capabilities(context, api, session_id):
    _validate_session_id(session_id)
    global_callables, global_errors = _collect_qmt_side(api, True)
    context_callables, context_errors = _collect_qmt_side(context, False)
    stages = {"inventory": "complete"}
    for name in STAGE_NAMES:
        stages[name] = "not_run"
    return {
        "protocol": PROTOCOL,
        "session_id": session_id,
        "state": "incomplete",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pid": os.getpid(),
        "captured_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "modules": _collect_modules(),
        "qmt_callables": {
            "globals": global_callables,
            "ContextInfo": context_callables,
        },
        "qmt_errors": {
            "globals": global_errors,
            "ContextInfo": context_errors,
        },
        "stages": stages,
    }


def write_report(report, report_dir):
    session_id = report.get("session_id") if isinstance(report, dict) else None
    _validate_session_id(session_id)
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, session_id + ".json")
    with open(path, "x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    return path


def init(C):
    try:
        report = collect_capabilities(C, globals(), SESSION_ID)
        path = write_report(report, REPORT_DIR)
    except Exception as error:
        print("QMT memory inventory failed: " + _error_text(error))
        raise
    print("QMT memory inventory saved: " + path)


def handlebar(C):
    pass


def stop(C):
    pass
