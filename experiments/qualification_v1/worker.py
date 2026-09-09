"""Throwaway P0/P1 harness, NOT a production bridge or a trading endpoint."""
import math
import os
import sys
import time

from qmt_bridge.protocol import check_id, load_json, publish_result

PROTOCOL = 'qualification-v1'
SAMPLE_CODES = ('000001.SZ', '510300.SH', '000300.SH')
SAMPLE_DATE = '20260908'
PERIODS = ('1d', '1m', '5m', 'tick')


def dispatch(context, api, request):
    if request.get('protocol') != PROTOCOL:
        raise ValueError('wrong qualification protocol')
    check_id(request.get('request_id'))
    deadline = float(request['deadline'])
    if not math.isfinite(deadline) or time.time() >= deadline:
        raise ValueError('request expired')
    operation, args = request.get('operation'), request.get('args')
    if not isinstance(args, dict):
        raise ValueError('args must be an object')
    if operation not in ('capabilities', 'modules', 'read_history', 'download_history', 'full_tick'):
        raise ValueError('operation not allowed in qualification harness')
    if operation == 'capabilities':
        names = ('download_history_data', 'down_history_data', 'download_history_data2',
                 'download_financial_data', 'download_financial_data2', 'get_sector_list',
                 'get_trade_detail_data', 'passorder', 'cancel', 'get_full_tick',
                 'get_market_data_ex_ori', 'get_market_data_ex', 'get_raw_financial_data',
                 'subscribe_quote', 'subscribe_whole_quote', 'set_account')
        return {'protocol': PROTOCOL, 'pid': os.getpid(), 'python': sys.version,
                'source': __file__, 'trading_enabled': False,
                'globals': {n: callable(api.get(n)) for n in names},
                'context': {n: callable(getattr(context, n, None)) for n in names}}
    if operation == 'modules':
        result = {}
        for name in ('importlib', 'socket', 'ctypes', 'queue', 'threading', 'redis', 'zmq', 'pandas', 'numpy'):
            try:
                module = __import__(name)
                result[name] = {'importable': True, 'version': str(getattr(module, '__version__', ''))[:100]}
            except Exception as exc:
                result[name] = {'importable': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
        return result
    code = args.get('stock_code')
    if code not in SAMPLE_CODES:
        raise ValueError('stock_code outside qualification sample')
    if operation == 'full_tick':
        return context.get_full_tick([code])
    period, date = args.get('period'), args.get('date')
    if period not in PERIODS or date != SAMPLE_DATE:
        raise ValueError('period/date outside qualification sample')
    if operation == 'download_history':
        name = next((n for n in ('download_history_data', 'down_history_data')
                     if callable(api.get(n))), None)
        if name is None:
            raise NotImplementedError('no injected history downloader; native xtdata is not a fallback')
        started = time.monotonic()
        result = api[name](code, period, date, date)
        if result is False or (isinstance(result, (int, float)) and result < 0):
            raise RuntimeError('download request rejected: %r' % result)
        return {'state': 'request_returned', 'data_ready': False, 'api': name,
                'elapsed_seconds': time.monotonic() - started, 'return_value': result}
    fields = [] if period == 'tick' else ['open', 'high', 'low', 'close', 'volume', 'amount']
    reader = getattr(context, 'get_market_data_ex_ori', None)
    if not callable(reader):
        raise NotImplementedError('raw reader unavailable; do not alter history semantics')
    result = reader(fields=fields, stock_code=[code], period=period,
                    start_time=date, end_time=date, count=-1,
                    dividend_type='none', fill_data=False, subscribe=False)
    return {'__qmt_raw_market__': 1, 'fields': fields, 'data': result}


class QualificationWorker:
    """Separate queue. Never replay interrupted work, never dispatch trades."""
    def __init__(self, root, context, api):
        self.root, self.context, self.api = os.path.abspath(root), context, api
        for name in ('requests', 'running', 'responses'):
            os.makedirs(os.path.join(self.root, name), exist_ok=True)
        self.lock = open(os.path.join(self.root, 'qualification.lock'), 'a+b')
        try:
            import msvcrt
            self.lock.seek(0)
            if not self.lock.read(1):
                self.lock.write(b'0')
                self.lock.flush()
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def poll(self):
        if self.lock is None:
            raise RuntimeError('qualification worker closed')
        names = sorted(n for n in os.listdir(os.path.join(self.root, 'requests'))
                       if n.endswith('.json'))
        if not names:
            return False
        name = names[0]
        rid = name[:-5]
        check_id(rid)
        running = os.path.join(self.root, 'running', name)
        responses = os.path.join(self.root, 'responses')
        if os.path.exists(running) or os.path.exists(os.path.join(responses, name)):
            raise RuntimeError('duplicate request file; investigate without replay')
        os.replace(os.path.join(self.root, 'requests', name), running)
        try:
            req = load_json(running)
            if req.get('request_id') != rid:
                raise ValueError('request id mismatch')
            result = dispatch(self.context, self.api, req)
            publish_result(responses, rid, {'experiment': PROTOCOL, 'data': result,
                           'finished_after_deadline': time.time() >= float(req['deadline'])}, None)
        except Exception as exc:
            publish_result(responses, rid, None, '%s: %s' % (type(exc).__name__, exc))
        os.unlink(running)
        return True
