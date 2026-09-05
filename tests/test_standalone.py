import importlib.util
import json
import subprocess
import sys

import pytest


def test_package_exists_without_parent_project():
    assert importlib.util.find_spec('bigqmt_bridge') is not None


def test_config_is_explicit_and_does_not_load_parent_settings(tmp_path, monkeypatch):
    from bigqmt_bridge.config import load_config
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.yaml').write_text('not: a bridge config', encoding='utf-8')
    monkeypatch.setenv('DATA_COLLECT_QMT_BACKEND', 'native')
    assert load_config() == {}
    config = tmp_path / 'bridge.json'
    config.write_text(json.dumps({'bridge_dir': str(tmp_path / 'ipc'), 'cache_prepared': True}))
    assert load_config(config)['cache_prepared'] is True


@pytest.mark.parametrize('value', [{'cache_prepared': 'false'}, {'unexpected': 'x'}, [],
                                 {'backend': 'bigqmt_rpc'}, {'timeout': 0}])
def test_config_rejects_unsafe_or_unsupported_values(tmp_path, value):
    from bigqmt_bridge.config import load_config
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_config(path)


def test_financial_resource_loads_outside_source_tree(tmp_path, monkeypatch):
    from bigqmt_bridge.normalize import financial_schema
    monkeypatch.chdir(tmp_path)
    financial_schema.cache_clear()
    schema = financial_schema()
    assert len(schema) == 8
    assert len(schema['Balance']['fields']) == 159
    assert schema['Top10holder']['fields'][-2:] == ['declareDate', 'endDate']


def test_relative_ipc_directory_is_resolved_against_config_not_cwd(tmp_path, monkeypatch):
    from bigqmt_bridge.config import load_config
    directory = tmp_path / 'settings'
    directory.mkdir()
    config = directory / 'bridge.json'
    config.write_text('{"bridge_dir": "ipc"}')
    monkeypatch.chdir(tmp_path)
    assert load_config(config)['bridge_dir'] == str((directory / 'ipc').resolve())


def test_cli_explicit_directory_overrides_config(tmp_path):
    from bigqmt_bridge.cli import main
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'bridge_dir': str(tmp_path / 'unused'), 'timeout': 60}))
    output = tmp_path / 'failure.json'
    assert main(['probe', '--config', str(config), '--bridge-dir', str(tmp_path / 'chosen'),
                 '--timeout', '0.01', '--output', str(output)]) == 1
    assert not (tmp_path / 'unused').exists()
    assert len(list((tmp_path / 'chosen' / 'requests').glob('*.json'))) == 1


def test_cli_no_worker_fails_and_writes_evidence_from_explicit_config(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'bridge_dir': str(tmp_path / 'ipc'), 'timeout': 0.05}))
    output = tmp_path / 'evidence.json'
    result = subprocess.run([sys.executable, '-m', 'bigqmt_bridge', 'probe',
                             '--config', str(config), '--output', str(output)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    report = json.loads(output.read_text())
    assert report['ok'] is False
    assert 'timeout' in report['errors'][0]
    assert len(list((tmp_path / 'ipc' / 'requests').glob('*.json'))) == 1
