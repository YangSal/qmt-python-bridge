"""Explicit, local JSON configuration. No parent-project or environment discovery."""
import json
import math
from pathlib import Path


def validate_config(config):
    allowed = {'backend', 'bridge_dir', 'cache_prepared', 'timeout', 'poll_interval',
               'batch_size', 'sector_root', 'instrument_fields', 'index_sectors'}
    if not isinstance(config, dict):
        raise ValueError('bridge config must be a flat JSON object')
    unknown = set(config) - allowed
    if unknown:
        raise ValueError('unknown bridge config keys: ' + ', '.join(sorted(unknown)))
    if config.get('backend', 'file_bridge') not in ('file_bridge', 'native'):
        raise ValueError('backend must be file_bridge or native (baseline CLI only)')
    if 'cache_prepared' in config and type(config['cache_prepared']) is not bool:
        raise ValueError('cache_prepared must be a JSON boolean')
    for key in ('timeout', 'poll_interval'):
        if key in config:
            value = config[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(key + ' must be a finite positive number')
    if 'batch_size' in config:
        value = config['batch_size']
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError('batch_size must be an integer from 1 to 10')
    for key in ('bridge_dir', 'sector_root'):
        if key in config and not isinstance(config[key], str):
            raise ValueError(key + ' must be a string')
    if 'bridge_dir' in config and not config['bridge_dir'].strip():
        raise ValueError('bridge_dir must not be empty')
    fields = config.get('instrument_fields', [])
    if not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields):
        raise ValueError('instrument_fields must be a list of nonempty strings')
    sectors = config.get('index_sectors', {})
    if not isinstance(sectors, dict) or not all(isinstance(k, str) and k and
            isinstance(v, str) and v for k, v in sectors.items()):
        raise ValueError('index_sectors must map codes to nonempty verified sector names')


def load_config(path=None):
    if path is None:
        return {}
    location = Path(path).resolve()
    config = json.loads(location.read_text(encoding='utf-8-sig'))
    validate_config(config)
    if 'bridge_dir' in config:
        directory = Path(config['bridge_dir'])
        if not directory.is_absolute():
            config['bridge_dir'] = str((location.parent / directory).resolve())
    return config
