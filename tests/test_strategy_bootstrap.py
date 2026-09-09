"""Run the actual strategy in a fresh process with a reduced import surface."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('missing_module', ['importlib', 'broken_dependency'])
def test_strategy_handles_only_missing_optional_reload_dependency(tmp_path, missing_module):
    # Requiring importlib breaks startup on the simulation terminal. Silencing
    # an unrelated broken dependency would hide a different installation error.
    source_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-I', '-S', '-c', r'''
import builtins
import sys

source_root, runtime_root, missing_module = sys.argv[1:]
sys.path.insert(0, source_root)
original_import = builtins.__import__

def reduced_import(name, *args, **kwargs):
    if name == 'importlib' or name.startswith('importlib.'):
        raise ModuleNotFoundError("No module named '%s'" % missing_module,
                                  name=missing_module)
    return original_import(name, *args, **kwargs)

builtins.__import__ = reduced_import
with open(source_root + '/qmt_bridge/strategy.py', encoding='ascii') as stream:
    code = compile(stream.read(), 'strategy.py', 'exec')
namespace = {'__name__': 'qmt_strategy_test'}
if missing_module != 'importlib':
    try:
        exec(code, namespace)
    except ModuleNotFoundError as error:
        assert error.name == missing_module
    else:
        raise AssertionError('unrelated import failure was hidden')
    sys.exit(0)

exec(code, namespace)
namespace['BRIDGE_DIR'] = runtime_root

class Context:
    def run_time(self, *args):
        self.timer = args

context = Context()
try:
    namespace['init'](context)
    assert context.timer == ('bridge_poll', '2nSecond', '2020-01-01 00:00:00')
    from qmt_bridge.protocol import atomic_json, read_result
    import time
    request_id = 'a' * 32
    atomic_json(runtime_root + '/requests/' + request_id + '.json', {
        'protocol': 1, 'request_id': request_id, 'operation': 'probe',
        'args': {}, 'deadline': time.time() + 30,
    })
    namespace['bridge_poll'](context)
    response = read_result(runtime_root + '/responses', request_id)
    assert response['protocol'] == 1
    assert response['market_reader'] == 'get_market_data_ex'
    first = namespace['_worker']
    namespace['init'](context)
    assert first._lock is None
    namespace['bridge_poll'](context)
finally:
    namespace['stop'](context)
assert namespace['_worker'] is None
''', str(source_root), str(tmp_path), missing_module],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if missing_module == 'importlib':
        assert 'QMT data bridge ready:' in result.stdout
        assert 'restart QMT' in result.stdout
