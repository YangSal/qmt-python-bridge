import datetime

import pytest


def test_request_hash_is_canonical_and_binds_operation():
    from qmt_bridge.auto_protocol import request_hash

    left = request_hash('probe', {'b': [2, 1], 'a': 1})
    right = request_hash('probe', {'a': 1, 'b': [2, 1]})
    assert left == right
    assert len(left) == 64
    assert request_hash('market_data', {'a': 1, 'b': [2, 1]}) != left


@pytest.mark.parametrize('args', [
    {},
    {'stock_code': '000001', 'period': '1d', 'date': '20200102'},
    {'stock_code': '000001.XSHE', 'period': '1d', 'date': '20200102'},
    {'stock_code': '00001.SZ', 'period': '1d', 'date': '20200102'},
    {'stock_code': '000001.SZ', 'period': 'tick', 'date': '20200102'},
    {'stock_code': '000001.SZ', 'period': '1d', 'date': '2020-01-02'},
    {'stock_code': '000001.SZ', 'period': '1d', 'date': '20200230'},
    {'stock_code': '000001.SZ', 'period': '1d', 'date': '29990101'},
    {'stock_code': '000001.SZ\n', 'period': '1d', 'date': '20200102'},
    {'stock_code': '000001.SZ', 'period': '1d', 'date': '20200102\n'},
    {'stock_code': '000001.SZ', 'period': '1d', 'date': '20200102', 'extra': True},
])
def test_validate_download_rejects_malformed_or_non_past_requests(args):
    from qmt_bridge.auto_protocol import validate_download

    with pytest.raises(ValueError):
        validate_download(args)


def test_validate_download_returns_the_strict_single_day_command():
    from qmt_bridge.auto_protocol import validate_download

    assert validate_download({
        'stock_code': '000001.SZ', 'period': '5m', 'date': '20200102'
    }) == ('000001.SZ', '5m', '20200102')


def test_file_lock_is_nonblocking_and_reusable(tmp_path):
    from qmt_bridge.auto_protocol import FileLock

    path = tmp_path / 'nested' / 'bridge.lock'
    first = FileLock(path)
    try:
        with pytest.raises((OSError, RuntimeError)):
            FileLock(path)
    finally:
        first.close()
    with FileLock(path):
        assert path.exists()


def test_protocol_declares_download_without_mutating_old_whitelist():
    from qmt_bridge import protocol as old
    from qmt_bridge.auto_protocol import OPERATIONS, PROTOCOL

    assert PROTOCOL == 'auto-kline-v1'
    assert OPERATIONS == old.OPERATIONS | {'download_kline'}
    assert 'download_kline' not in old.OPERATIONS
