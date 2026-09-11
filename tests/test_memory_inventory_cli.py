import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
INSPECTOR_PATH = ROOT / "experiments" / "memory_v1" / "inspect_report.py"
INVENTORY_PATH = ROOT / "experiments" / "memory_v1" / "strategy_inventory.py"
SESSION = "a" * 32


def load(path, name):
    assert path.is_file(), "%s is missing" % path.name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inspector():
    return load(INSPECTOR_PATH, "memory_inventory_inspector")


@pytest.fixture
def valid_report():
    inventory = load(INVENTORY_PATH, "memory_inventory_entry_for_cli")
    return inventory.collect_capabilities(object(), {}, SESSION)


def test_valid_report_stays_explicitly_unverified(inspector, valid_report):
    result = inspector.validate_report(valid_report, SESSION)
    assert result["protocol"] == "memory-inventory-v1"
    assert result["session_id"] == SESSION
    assert result["state"] == "incomplete"
    assert result["transport_verified"] is False
    assert result["stages"] == {
        "inventory": "complete", "cross_process": "not_run",
        "synchronization": "not_run", "security": "not_run",
        "load": "not_run", "recovery": "not_run", "scheduling": "not_run",
    }
    assert "python" not in result
    assert "qmt_errors" not in result


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(protocol="wrong"),
    lambda r: r.pop("pid"),
    lambda r: r.update(session_id="b" * 32),
    lambda r: r.update(state="shared_memory_verified"),
    lambda r: r["stages"].pop("security"),
    lambda r: r["stages"].update(security="complete"),
    lambda r: r["modules"]["mmap"]["callables"].update(mmap=1),
    lambda r: r.update(extra_payload={"quotes": []}),
])
def test_rejects_schema_or_verification_claims(inspector, valid_report, mutation):
    mutation(valid_report)
    with pytest.raises(ValueError):
        inspector.validate_report(valid_report, SESSION)


def test_rejects_module_inconsistency_and_bad_errors(inspector, valid_report):
    report = copy.deepcopy(valid_report)
    report["modules"]["mmap"]["importable"] = False
    report["modules"]["mmap"]["callables"]["mmap"] = True
    with pytest.raises(ValueError):
        inspector.validate_report(report, SESSION)

    report = copy.deepcopy(valid_report)
    report["qmt_errors"]["globals"]["run_time"] = "x" * 241
    with pytest.raises(ValueError):
        inspector.validate_report(report, SESSION)


@pytest.mark.parametrize("field,value", [
    ("pid", 0), ("pid", True), ("captured_at_utc", "NaN"),
    ("captured_at_utc", "2026-01-01 00:00:00Z"),
    ("python", ""), ("platform", "x" * 257),
])
def test_rejects_invalid_bounded_scalar(inspector, valid_report, field, value):
    valid_report[field] = value
    with pytest.raises(ValueError):
        inspector.validate_report(valid_report, SESSION)


def run_cli(path):
    return subprocess.run(
        [sys.executable, str(INSPECTOR_PATH), "--report", str(path),
         "--session-id", SESSION], text=True, capture_output=True,
    )


def test_cli_accepts_task1_report_and_emits_sanitized_json(tmp_path, valid_report):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(valid_report), encoding="utf-8")
    completed = run_cli(path)
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["transport_verified"] is False
    assert output["state"] == "incomplete"
    assert "python" not in output
    assert "error" not in completed.stdout.lower()


@pytest.mark.parametrize("payload", [
    "[]", '{"protocol":"memory-inventory-v1","protocol":"wrong"}',
    '{"pid":NaN}', '{"pid":Infinity}', "{not json",
])
def test_cli_rejects_malformed_json_without_echoing_input(tmp_path, payload):
    path = tmp_path / "report.json"
    path.write_text(payload, encoding="utf-8")
    completed = run_cli(path)
    assert completed.returncode == 1
    assert payload not in completed.stderr


def test_cli_rejects_report_larger_than_64_kib(tmp_path):
    path = tmp_path / "report.json"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    completed = run_cli(path)
    assert completed.returncode == 1
    assert completed.stderr.strip() == "inventory report validation failed"
