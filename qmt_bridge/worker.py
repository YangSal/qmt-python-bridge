"""One request per QMT timer callback; no network, threads, or trading."""
import os
import sys
import time
import uuid

from .protocol import (PROTOCOL, OPERATIONS, MAX_REQUEST_BYTES,
                       check_id, load_json, publish_result)


def dispatch(context, api, operation, args):
    if operation not in OPERATIONS:
        raise ValueError('operation not allowed')
    if operation == 'probe':
        names = ('get_market_data_ex', 'get_divid_factors', 'get_raw_financial_data',
                 'get_instrument_detail', 'get_weight_in_index', 'get_stock_list_in_sector')
        return {'protocol': PROTOCOL, 'python': sys.version, 'pid': os.getpid(),
                'worker_version': 2,
                'market_reader': ('get_market_data_ex_ori' if callable(getattr(context, 'get_market_data_ex_ori', None)) else 'get_market_data_ex'),
                'capabilities': {n: callable(getattr(context, n, None)) for n in names},
                'sector_tree': callable(api.get('get_sector_list')),
                'server_time_utc': time.time()}
    stocks = args.get('stock_list', [])
    if not isinstance(stocks, list) or len(stocks) > 10:
        raise ValueError('stock_list limit is 10')
    if operation == 'market_data':
        if args.get('subscribe', False) is not False or args.get('fill_data', False) is not False:
            raise ValueError('only unsubscribed, unfilled history is allowed')
        if args.get('period') not in ('1d', '1m', '5m', 'tick'):
            raise ValueError('unsupported history period')
        if args.get('period') == 'tick' and len(stocks) > 1:
            raise ValueError('tick requests must contain one stock')
        raw_reader = getattr(context, 'get_market_data_ex_ori', None)
        use_raw = callable(raw_reader)
        fields = args.get('fields', [])
        if use_raw:
            # QMT's raw wrapper provides stime; external normalization builds UTC time.
            fields = [f for f in fields if f != 'time']
        reader = raw_reader if use_raw else context.get_market_data_ex
        result = reader(
            fields=fields, stock_code=stocks, period=args['period'],
            start_time=args.get('start_time', ''), end_time=args.get('end_time', ''),
            count=args.get('count', -1), dividend_type=args.get('dividend_type', 'none'),
            fill_data=False, subscribe=False)
        return {'__qmt_raw_market__': 1, 'fields': fields, 'data': result} if use_raw else result
    if operation == 'divid_factors':
        # Omitting date returns ALL events per official API; filter externally.
        return context.get_divid_factors(args['stock_code'])
    if operation == 'instrument':
        return context.get_instrument_detail(args['stock_code'])
    if operation == 'sector_stocks':
        return context.get_stock_list_in_sector(args['sector_name'])
    if operation == 'weights':
        return {c: context.get_weight_in_index(args['index_code'], c) for c in stocks}
    if operation == 'financial':
        if len(stocks) != 1:
            raise ValueError('financial requests must contain one stock')
        return context.get_raw_financial_data(
            args['fields'], stocks, args['start_time'], args['end_time'],
            report_type=args.get('report_type', 'report_time'), data_type='dict')
    if operation == 'sectors':
        get_tree = api.get('get_sector_list')
        if not callable(get_tree):
            raise ValueError('QMT global get_sector_list is unavailable')
        pending, visited, sectors = [args.get('root', '')], set(), set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            if len(visited) > 1000:
                raise ValueError('sector tree exceeds node limit')
            result = get_tree(node)
            if not isinstance(result, (list, tuple)) or len(result) != 2:
                raise ValueError('unexpected sector tree shape')
            leaves, children = result
            if not isinstance(leaves, (list, tuple)) or not isinstance(children, (list, tuple)):
                raise ValueError('unexpected sector node shape')
            if not all(isinstance(s, str) for s in list(leaves) + list(children)):
                raise ValueError('sector names must be strings')
            sectors.update(leaves)
            pending.extend(children)
        if not sectors:
            raise ValueError('sector tree is empty; select the actual terminal root')
        return sorted(sectors)


class Worker:
    def __init__(self, root, context, api):
        self.root, self.context, self.api = os.path.abspath(root), context, api
        self._lock = None
        for name in ('requests', 'running', 'responses'):
            os.makedirs(os.path.join(self.root, name), exist_ok=True)
        self._lock = open(os.path.join(self.root, 'worker.lock'), 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._lock.seek(0)
                if not self._lock.read(1):
                    self._lock.write(b'0')
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self._lock.close()
            self._lock = None
            raise
        # The OS lock is released on process death. Only its new owner recovers.
        for name in os.listdir(os.path.join(self.root, 'running')):
            if name.endswith('.json'):
                os.replace(os.path.join(self.root, 'running', name),
                           os.path.join(self.root, 'requests', name))

    def close(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def poll(self):
        if self._lock is None:
            raise RuntimeError('worker is closed')
        names = sorted(n for n in os.listdir(os.path.join(self.root, 'requests')) if n.endswith('.json'))
        if not names:
            return False
        name = names[0]
        request_id = name[:-5]
        try:
            check_id(request_id)
        except ValueError:
            quarantine = os.path.join(self.root, 'quarantine')
            os.makedirs(quarantine, exist_ok=True)
            os.replace(os.path.join(self.root, 'requests', name),
                       os.path.join(quarantine, uuid.uuid4().hex + '.json'))
            return True
        running = os.path.join(self.root, 'running', name)
        os.replace(os.path.join(self.root, 'requests', name), running)
        responses = os.path.join(self.root, 'responses')
        if os.path.exists(os.path.join(responses, name)):
            os.unlink(running)
            return True
        try:
            req = load_json(running, MAX_REQUEST_BYTES)
            if req.get('protocol') != PROTOCOL or req.get('request_id') != request_id:
                raise ValueError('request protocol/request_id mismatch')
            if not isinstance(req.get('args'), dict):
                raise ValueError('args must be an object')
            deadline = float(req['deadline'])
            if not time.time() < deadline:
                raise ValueError('request expired')
            result = dispatch(self.context, self.api, req['operation'], req['args'])
            if time.time() >= deadline:
                raise ValueError('request expired during QMT call')
            publish_result(responses, request_id, result, None)
        except Exception as exc:
            publish_result(responses, request_id, None, '%s: %s' % (type(exc).__name__, exc))
        os.unlink(running)
        return True
