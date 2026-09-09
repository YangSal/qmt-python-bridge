"""Automatic K-line facade; metadata and legacy cache contracts stay inherited."""
import pandas as pd

from . import QmtDataError
from .backend import InnerBackend, bridge_errors
from .downloads import DownloadManager
from .kline import original_frames


class AutomaticBackend(InnerBackend):
    def __init__(self, transport, config):
        super().__init__(transport, config)
        self.downloads = DownloadManager(transport, config)

    def _history_cache(self):
        pass

    def _normalize_market(self, raw, codes):
        frames = original_frames(raw, codes)
        for frame in frames.values():
            # The legacy normalizer also accepts calendar-shaped integers.
            # Give it explicit UTC values built only from validated epoch ms,
            # with fixed precision so mixed millisecond values parse uniformly.
            frame['time'] = pd.to_datetime(frame['time'].tolist(), unit='ms', utc=True).map(
                lambda value: value.isoformat(timespec='milliseconds'))
        return super()._normalize_market(frames, codes)

    def download_history_data2(self, stock_list, period, start_time='', end_time='', *,
                               expected_dates=None, job_id=None, callback=None):
        return self.downloads.download(stock_list, period, start_time, end_time,
                                       expected_dates, job_id, callback)

    def download_status(self, job_id):
        return self.downloads.status(job_id)

    def download_financial_data2(self, stock_list, table_list=None, **kwargs):
        raise QmtDataError('automatic financial downloads are not supported')

    def download_index_weight(self):
        raise QmtDataError('automatic index-weight downloads are not supported')

    @bridge_errors
    def get_market_data_ex(self, field_list=None, stock_list=None, period='1d', start_time='',
                           end_time='', count=-1, dividend_type='none', fill_data=False,
                           subscribe=False):
        if period == 'tick':
            raise QmtDataError('automatic Tick reads are unsupported: field contract is not complete')
        if period not in ('1d', '1m', '5m') or dividend_type != 'none':
            raise QmtDataError('automatic reads require 1d/1m/5m and dividend_type=none')
        fields = list(field_list or [])
        if fields and 'time' not in fields:
            fields.insert(0, 'time')
        return super().get_market_data_ex(fields, stock_list, period, start_time, end_time,
                                          count, dividend_type, fill_data, subscribe)
