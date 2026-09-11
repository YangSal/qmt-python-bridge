"""Strict offline validator for a memory inventory report."""
import argparse
import datetime
import json
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy_inventory import (  # noqa: E402
    MAX_ERROR_LENGTH, MODULE_CALLABLES, PROTOCOL, QMT_CALLABLE_NAMES,
    STAGE_NAMES,
)


MAX_REPORT_BYTES = 64 * 1024
TOP_LEVEL_KEYS = {
    "protocol", "session_id", "state", "python", "platform", "pid",
    "captured_at_utc", "modules", "qmt_callables", "qmt_errors", "stages",
}
QMT_SIDES = ("globals", "ContextInfo")


def _fail(message):
    raise ValueError(message)


def _exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        _fail("invalid %s schema" % label)


def _bool_map(value, names, label):
    _exact_keys(value, names, label)
    if any(type(value[name]) is not bool for name in names):
        _fail("invalid %s boolean" % label)


def _bounded_error(value, label, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > MAX_ERROR_LENGTH:
        _fail("invalid %s" % label)


def _validate_modules(modules):
    fixed = dict(MODULE_CALLABLES)
    _exact_keys(modules, fixed, "modules")
    for module_name, callable_names in MODULE_CALLABLES:
        item = modules[module_name]
        _exact_keys(item, ("importable", "error", "callables"),
                    "module %s" % module_name)
        if type(item["importable"]) is not bool:
            _fail("invalid module importable flag")
        _bounded_error(item["error"], "module error", nullable=True)
        _bool_map(item["callables"], callable_names,
                  "module %s callables" % module_name)
        if not item["importable"]:
            if item["error"] is None or any(item["callables"].values()):
                _fail("inconsistent module import result")


def _validate_qmt(callables, errors):
    _exact_keys(callables, QMT_SIDES, "qmt_callables")
    _exact_keys(errors, QMT_SIDES, "qmt_errors")
    for side in QMT_SIDES:
        _bool_map(callables[side], QMT_CALLABLE_NAMES,
                  "qmt_callables.%s" % side)
        side_errors = errors[side]
        if not isinstance(side_errors, dict):
            _fail("invalid qmt_errors.%s" % side)
        if not set(side_errors).issubset(set(QMT_CALLABLE_NAMES)):
            _fail("unexpected qmt error name")
        for name, error in side_errors.items():
            _bounded_error(error, "qmt error")
            if callables[side][name]:
                _fail("callable cannot also have an error")


def _validate_timestamp(value):
    if (not isinstance(value, str) or not value.endswith("Z") or
            len(value) > 40 or len(value) < 21 or value[10] != "T"):
        _fail("invalid captured_at_utc")
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError):
        _fail("invalid captured_at_utc")
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        _fail("invalid captured_at_utc")


def validate_report(report, expected_session):
    _exact_keys(report, TOP_LEVEL_KEYS, "report")
    if report["protocol"] != PROTOCOL:
        _fail("unsupported protocol")
    if (not isinstance(expected_session, str) or len(expected_session) != 32 or
            any(c not in "0123456789abcdef" for c in expected_session)):
        _fail("invalid expected session")
    if report["session_id"] != expected_session:
        _fail("session mismatch")
    if report["state"] != "incomplete":
        _fail("inventory must remain incomplete")
    for label in ("python", "platform"):
        value = report[label]
        if not isinstance(value, str) or not value or len(value) > 256:
            _fail("invalid %s" % label)
    if type(report["pid"]) is not int or report["pid"] <= 0:
        _fail("invalid pid")
    _validate_timestamp(report["captured_at_utc"])
    _validate_modules(report["modules"])
    _validate_qmt(report["qmt_callables"], report["qmt_errors"])

    expected_stages = {"inventory": "complete"}
    expected_stages.update((name, "not_run") for name in STAGE_NAMES)
    _exact_keys(report["stages"], expected_stages, "stages")
    if report["stages"] != expected_stages:
        _fail("invalid stage status")

    return {
        "protocol": PROTOCOL,
        "session_id": expected_session,
        "state": "incomplete",
        "transport_verified": False,
        "modules": {
            name: {
                "importable": report["modules"][name]["importable"],
                "callables": dict(report["modules"][name]["callables"]),
            } for name, _ in MODULE_CALLABLES
        },
        "qmt_callables": {
            side: dict(report["qmt_callables"][side]) for side in QMT_SIDES
        },
        "stages": dict(report["stages"]),
    }


def _no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def load_report(path):
    with open(path, "rb") as source:
        data = source.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("report too large")
    return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_pairs,
                      parse_constant=_reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a QMT memory inventory report")
    parser.add_argument("--report", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    try:
        output = validate_report(load_report(args.report), args.session_id)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        print("inventory report validation failed", file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
