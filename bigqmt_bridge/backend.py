"""Small xtdata-compatible facade used by existing collection jobs."""
import math
from functools import wraps

from . import QmtDataError
from .normalize import (financial_schema, normalize_divid, normalize_financial,
                        normalize_market)


def bridge_errors(function):
    """Never let malformed wire data fall into legacy per-stock skip handlers."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except QmtDataError:
            raise
        except Exception as exc:
            raise QmtDataError('%s: %s: %s' % (function.__name__, type(exc).__name__, exc)) from exc
    return wrapped


class InnerBackend:
    def __init__(self, transport, config):
        self.transport, self.config = transport, config
        self.batch_size = int(config.get('batch_size', 10))
        if not 1 <= self.batch_size <= 10:
            raise ValueError('batch_size must be 1..10')

    @bridge_errors
    def probe(self):
        return self.transport.call('probe', {})

    def _cache(self):
        if self.config.get('cache_prepared') is not True:
            raise QmtDataError('set cache_prepared=true only after QMT GUI history/financial download')

    def download_history_data2(self, stock_list, period, start_time='', end_time='', **kwargs):
        self._cache()

    def download_financial_data2(self, stock_list, table_list=None, **kwargs):
        self._cache()

    def download_index_weight(self):
        self._cache()

    @bridge_errors
    def get_market_data_ex(self, field_list=None, stock_list=None, period='1d', start_time='',
                           end_time='', count=-1, dividend_type='none', fill_data=False,
                           subscribe=False):
        if subscribe is not False or fill_data is not False:
            raise ValueError('migration reads require subscribe=False and fill_data=False')
        self._cache()
        codes = list(stock_list or [])
        batch_size = 1 if period == 'tick' else self.batch_size
        result = {}
        for offset in range(0, len(codes), batch_size):
            batch = codes[offset:offset + batch_size]
            raw = self.transport.call('market_data', {
                'fields': list(field_list or []), 'stock_list': batch, 'period': period,
                'start_time': start_time, 'end_time': end_time, 'count': count,
                'dividend_type': dividend_type, 'fill_data': False, 'subscribe': False})
            frames = normalize_market(raw, batch)
            for code, frame in frames.items():
                required = set(field_list or [])
                if period == 'tick':
                    required.update(('time', 'lastPrice', 'volume', 'amount', 'askPrice', 'bidPrice', 'askVol', 'bidVol'))
                missing = required - set(frame.columns)
                if missing:
                    raise QmtDataError('market fields missing %s: %s' % (code, sorted(missing)))
                if period == 'tick':
                    for field in ('askPrice', 'bidPrice', 'askVol', 'bidVol'):
                        valid = frame[field].map(lambda v: isinstance(v, (list, tuple)) and len(v) == 5)
                        if not valid.all():
                            raise QmtDataError('invalid five-level book %s: %s' % (code, field))
                    # Every scalar persisted by the strict tick schema is required.
                    scalars = {'open', 'high', 'low', 'lastClose', 'pvolume', 'tickvol',
                               'stockStatus', 'openInt', 'lastSettlementPrice',
                               'settlementPrice', 'transactionNum', 'pe'}
                    if scalars - set(frame.columns):
                        raise QmtDataError('tick persisted scalar fields missing: ' + ','.join(sorted(scalars - set(frame.columns))))
                if start_time or end_time:
                    import pandas as pd
                    days = pd.to_datetime(frame.time, unit='ms', utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y%m%d')
                    if ((start_time and (days < start_time[:8]).any()) or
                            (end_time and (days > end_time[:8]).any())):
                        raise QmtDataError('market cache returned dates outside requested range: ' + code)
            result.update(frames)
        return result

    @bridge_errors
    def get_local_data(self, *args, data_dir=None, **kwargs):
        if data_dir is not None:
            raise QmtDataError('external data_dir cannot select QMT internal cache')
        return self.get_market_data_ex(*args, **kwargs)

    @bridge_errors
    def get_divid_factors(self, stock_code, start_time='', end_time=''):
        self._cache()
        raw = self.transport.call('divid_factors', {'stock_code': stock_code})
        if raw is None:
            raise QmtDataError('dividend API returned None')
        return normalize_divid(raw, start_time, end_time)

    @bridge_errors
    def get_instrument_detail(self, stock_code, iscomplete=False):
        required = self.config.get('instrument_fields', [])
        if iscomplete and not required:
            raise QmtDataError('complete instrument contract requires instrument_fields from native baseline')
        raw = self.transport.call('instrument', {'stock_code': stock_code})
        if not isinstance(raw, dict) or not raw:
            raise QmtDataError('instrument missing: ' + stock_code)
        if iscomplete and set(required) - set(raw):
            raise QmtDataError('incomplete instrument fields: ' + ','.join(sorted(set(required) - set(raw))))
        return raw

    @bridge_errors
    def get_sector_list(self):
        raw = self.transport.call('sectors', {'root': self.config.get('sector_root', '')})
        if not isinstance(raw, list) or not raw or not all(isinstance(v, str) for v in raw):
            raise QmtDataError('invalid/empty sector listing')
        return raw

    @bridge_errors
    def get_stock_list_in_sector(self, sector_name):
        raw = self.transport.call('sector_stocks', {'sector_name': sector_name})
        if not isinstance(raw, list) or not raw or not all(isinstance(v, str) for v in raw):
            raise QmtDataError('invalid sector membership: ' + sector_name)
        return raw

    @bridge_errors
    def get_index_weight(self, index_code):
        sector = self.config.get('index_sectors', {}).get(index_code)
        if not sector:
            raise QmtDataError('index_sectors must map index code to verified constituent sector: ' + index_code)
        codes = self.get_stock_list_in_sector(sector)
        if not codes:
            raise QmtDataError('empty index constituents: ' + index_code)
        result = {}
        for offset in range(0, len(codes), self.batch_size):
            batch = codes[offset:offset+self.batch_size]
            raw = self.transport.call('weights', {'index_code': index_code, 'stock_list': batch})
            if not isinstance(raw, dict) or set(raw) != set(batch):
                raise QmtDataError('incomplete index weights')
            result.update(raw)
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in result.values()):
            raise QmtDataError('invalid index weight values')
        # QMT documents absolute weights in percent; do not silently rescale.
        if not 95 <= sum(result.values()) <= 105:
            raise QmtDataError('index weights do not sum near 100; verify units/membership')
        return result

    @bridge_errors
    def get_financial_data(self, stock_list, table_list=None, start_time='', end_time='', report_type='report_time'):
        self._cache()
        schema = financial_schema()
        result = {}
        for code in stock_list:
            result[code] = {}
            for table in table_list or schema:
                if table not in schema:
                    raise QmtDataError('unknown financial table: ' + table)
                spec = schema[table]
                raw = self.transport.call('financial', {
                    'stock_list': [code], 'fields': [spec['prefix'] + '.' + f for f in spec['fields']],
                    'start_time': start_time or '19900101', 'end_time': end_time or '20991231',
                    'report_type': report_type})
                result[code][table] = normalize_financial(raw, code, table, spec['prefix'], spec['fields'])
        return result


def create_backend(config):
    from .transport import FileTransport
    from .config import validate_config
    validate_config(config)
    name = config.get('backend', 'file_bridge')
    if name != 'file_bridge':
        raise ValueError('unknown QMT bridge backend: ' + str(name))
    transport = FileTransport(config)
    return InnerBackend(transport, config)
