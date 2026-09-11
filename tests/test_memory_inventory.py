import ast
import builtins
import importlib.util
import json
import os
from pathlib import Path

import pytest


ENTRY = Path(__file__).parents[1] / "experiments" / "memory_v1" / "strategy_inventory.py"
MODULE_CALLABLES = {
    "mmap": ["mmap"],
    "_winapi": [
        "CreateNamedPipe", "ConnectNamedPipe", "CreateFile", "ReadFile",
        "WriteFile", "PeekNamedPipe", "SetNamedPipeHandleState", "CloseHandle",
        "WaitForSingleObject", "CreateMutex", "ReleaseMutex",
    ],
    "_multiprocessing": ["SemLock"],
    "ctypes": ["WinDLL"],
    "socket": ["socket"],
    "msvcrt": ["locking"],
}
QMT_NAMES = [
    "run_time", "schedule_run", "subscribe_quote", "subscribe_whole_quote",
    "get_full_tick", "get_trade_detail_data", "passorder", "cancel",
]


def load_entry():
    assert ENTRY.exists(), "Task 1 strategy inventory entry is missing"
    spec = importlib.util.spec_from_file_location("memory_inventory_under_test", str(ENTRY))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RaisingContext(object):
    def __getattribute__(self, name):
        if name == "cancel":
            raise RuntimeError("context attribute exploded " + ("x" * 1000))
        return object.__getattribute__(self, name)

    def passorder(self):
        raise AssertionError("QMT functions must not be invoked")


def test_collect_reports_fixed_inventory_and_isolates_errors(monkeypatch):
    inventory = load_entry()
    real_import = builtins.__import__

    class AttributeTrap(object):
        def __getattribute__(self, name):
            if name == "WriteFile":
                raise RuntimeError("attribute unavailable")
            if name == "ReadFile":
                return lambda: None
            return None

    def controlled_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "_multiprocessing":
            raise ImportError("broker omitted module")
        if name == "_winapi":
            return AttributeTrap()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", controlled_import)
    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("QMT functions must not be invoked")
    )
    report = inventory.collect_capabilities(
        RaisingContext(), {"passorder": forbidden}, "a" * 32
    )

    assert report["protocol"] == "memory-inventory-v1"
    assert report["session_id"] == "a" * 32
    assert report["state"] == "incomplete"
    assert set(report["modules"]) == set(MODULE_CALLABLES)
    for name, callable_names in MODULE_CALLABLES.items():
        item = report["modules"][name]
        assert set(item) == {"importable", "error", "callables"}
        assert list(item["callables"]) == callable_names
        assert all(isinstance(value, bool) for value in item["callables"].values())
    assert report["modules"]["_multiprocessing"]["importable"] is False
    assert report["modules"]["_multiprocessing"]["callables"]["SemLock"] is False
    assert "broker omitted module" in report["modules"]["_multiprocessing"]["error"]
    assert report["modules"]["_winapi"]["callables"]["ReadFile"] is True
    assert report["modules"]["_winapi"]["callables"]["WriteFile"] is False
    assert "WriteFile" in report["modules"]["_winapi"]["error"]
    assert list(report["qmt_callables"]["globals"]) == QMT_NAMES
    assert list(report["qmt_callables"]["ContextInfo"]) == QMT_NAMES
    assert report["qmt_callables"]["globals"]["passorder"] is True
    assert report["qmt_callables"]["ContextInfo"]["passorder"] is True
    assert report["qmt_callables"]["ContextInfo"]["cancel"] is False
    assert "cancel" in report["qmt_errors"]["ContextInfo"]
    assert len(report["qmt_errors"]["ContextInfo"]["cancel"]) <= 240
    assert report["stages"] == {
        "inventory": "complete", "cross_process": "not_run",
        "synchronization": "not_run", "security": "not_run",
        "load": "not_run", "recovery": "not_run", "scheduling": "not_run",
    }
    assert isinstance(report["pid"], int)
    assert isinstance(report["python"], str)
    assert isinstance(report["platform"], str)
    assert report["captured_at_utc"].endswith("Z")


@pytest.mark.parametrize("session_id", ["", "A" * 32, "a" * 31, "g" * 32, None])
def test_invalid_session_is_rejected(session_id, tmp_path):
    inventory = load_entry()
    with pytest.raises(ValueError):
        inventory.collect_capabilities(object(), {}, session_id)
    with pytest.raises(ValueError):
        inventory.write_report({"session_id": session_id}, str(tmp_path))


def test_write_report_is_exclusive_and_fsync_failure_propagates(tmp_path, monkeypatch):
    inventory = load_entry()
    report = inventory.collect_capabilities(object(), {}, "b" * 32)
    path = inventory.write_report(report, str(tmp_path))
    assert path == str(tmp_path / (("b" * 32) + ".json"))
    assert json.loads(Path(path).read_text(encoding="utf-8")) == report
    original = Path(path).read_bytes()
    with pytest.raises(FileExistsError):
        inventory.write_report(report, str(tmp_path))
    assert Path(path).read_bytes() == original

    report2 = dict(report, session_id="c" * 32)
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(OSError, match="fsync failed"):
        inventory.write_report(report2, str(tmp_path))


def test_init_prints_only_after_success_and_second_run_does_not_overwrite(tmp_path, monkeypatch, capsys):
    inventory = load_entry()
    inventory.SESSION_ID = "d" * 32
    inventory.REPORT_DIR = str(tmp_path)
    inventory.init(object())
    first = capsys.readouterr()
    path = tmp_path / (("d" * 32) + ".json")
    assert first.out.strip() == "QMT memory inventory saved: " + str(path)
    original = path.read_bytes()

    with pytest.raises(FileExistsError):
        inventory.init(object())
    second = capsys.readouterr()
    assert "saved" not in second.out
    assert "QMT memory inventory failed:" in second.out
    assert path.read_bytes() == original
    assert inventory.handlebar(object()) is None
    assert inventory.stop(object()) is None


def test_entry_parses_as_python_36_and_is_ascii():
    source = ENTRY.read_bytes()
    source.decode("ascii")
    ast.parse(source.decode("ascii"), feature_version=(3, 6))
