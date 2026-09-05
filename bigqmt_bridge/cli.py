"""Read-only migration evidence: python -m bigqmt_bridge --help."""
import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

from .config import load_config
from bigqmt_bridge import QmtDataError
from bigqmt_bridge.backend import create_backend
from bigqmt_bridge.normalize import (financial_schema, normalize_divid,
                                       normalize_financial, normalize_market, validate_market_values)
from qmt_bridge.protocol import MAX_BYTES, atomic_json, dumps, json_value, load_json

FAMILIES = ('market', 'divid', 'financial', 'instrument', 'sectors', 'weights')
BAR_FIELDS = ['time', 'open', 'high', 'low', 'close', 'volume', 'amount']


def frame_snapshot(frame):
    if frame.empty or not frame.columns.is_unique:
        raise QmtDataError('empty frame or duplicate columns cannot pass acceptance')
    columns = sorted(frame.columns)
    rows = json_value(frame[columns].to_dict('records'))
    # Preserve duplicate rows; canonical order is independent of API traversal.
    rows.sort(key=dumps)
    return {'columns': columns, 'rows': [[r[c] for c in columns] for r in rows]}


def collect_sample(source, date, codes, periods, families, sectors=None, indices=None, backend='file_bridge'):
    import pandas as pd
    if not re.fullmatch(r'\d{8}', date):
        raise ValueError('explicit Beijing date must be YYYYMMDD')
    pd.to_datetime(date, format='%Y%m%d', errors='raise')
    if not codes or len(codes) > 10 or len(codes) != len(set(codes)):
        raise ValueError('sample requires 1..10 unique codes')
    if not set(families) <= set(FAMILIES) or not families:
        raise ValueError('unknown/empty sample families')
    if not periods or not set(periods) <= {'1d', '1m', '5m', 'tick'}:
        raise ValueError('unknown/empty history periods')
    report = {'version': 1, 'date': date, 'codes': codes, 'periods': periods,
              'families': families, 'samples': {}, 'errors': [], 'seconds': {}}
    for family in families:
        started = time.monotonic()
        try:
            if family == 'market':
                data = {}
                for period in periods:
                    raw = {}
                    for code in codes:
                        reader = source.get_local_data if backend == 'native' else source.get_market_data_ex
                        params = dict(
                            field_list=[] if period == 'tick' else BAR_FIELDS,
                            stock_list=[code], period=period, start_time=date, end_time=date,
                            count=-1, dividend_type='none', fill_data=False)
                        if backend != 'native':
                            params['subscribe'] = False
                        raw.update(reader(**params))
                    frames = normalize_market(raw, codes)
                    for code, frame in frames.items():
                        if period != 'tick' and set(BAR_FIELDS) - set(frame.columns):
                            raise QmtDataError('missing bar fields: ' + code)
                        days = pd.to_datetime(frame.time, unit='ms', utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y%m%d')
                        if not days.eq(date).all():
                            raise QmtDataError('market sample date mismatch: ' + code)
                    data[period] = {c: frame_snapshot(f) for c, f in frames.items()}
            elif family == 'divid':
                data = {}
                for code in codes:
                    frame = normalize_divid(source.get_divid_factors(code, start_time='19900101', end_time=date), '19900101', date)
                    frame.index.name = 'event_date'
                    data[code] = frame_snapshot(frame.reset_index())
            elif family == 'financial':
                schema = financial_schema()
                raw = source.get_financial_data(codes, table_list=list(schema), start_time='19900101', end_time=date)
                data = {}
                for code in codes:
                    data[code] = {}
                    for table, spec in schema.items():
                        frame = normalize_financial({code: raw[code][table]}, code, table, spec['prefix'], spec['fields'])
                        data[code][table] = frame_snapshot(frame)
            elif family == 'instrument':
                data = {c: source.get_instrument_detail(c, iscomplete=True) for c in codes}
            elif family == 'sectors':
                data = {'all': sorted(source.get_sector_list()), 'members': {
                    s: sorted(source.get_stock_list_in_sector(s))
                    for s in (sectors or ['沪深A股', '沪深ETF', '沪深指数'])}}
            else:
                data = {c: source.get_index_weight(c) for c in (indices or ['000300.SH'])}
            report['samples'][family] = json_value(data)
        except Exception as exc:
            report['errors'].append('%s: %s: %s' % (family, type(exc).__name__, exc))
        report['seconds'][family] = round(time.monotonic() - started, 3)
    return report


def _validate(value, path, errors):
    if isinstance(value, dict):
        if not value:
            errors.append(path + ': empty object')
        if set(value) == {'columns', 'rows'}:
            cols, rows = value['columns'], value['rows']
            if not isinstance(cols, list) or not isinstance(rows, list) or not cols or not rows:
                errors.append(path + ': empty/invalid frame')
                return
            if len(set(cols)) != len(cols) or any(not isinstance(r, list) or len(r) != len(cols) for r in rows):
                errors.append(path + ': invalid frame shape')
        for key, child in value.items():
            _validate(child, path + '/' + str(key), errors)
    elif isinstance(value, list):
        if not value:
            errors.append(path + ': empty array')
        for i, child in enumerate(value):
            _validate(child, path + '/' + str(i), errors)


def compare(left, right):
    """Exact identity/array/schema comparison; only floats have 1e-9 tolerance."""
    errors = []
    for label, report in [('left', left), ('right', right)]:
        if report.get('version') != 1 or report.get('errors') != []:
            errors.append(label + ': failed or invalid sample report')
        if not report.get('families') or not report.get('codes'):
            errors.append(label + ': missing sample scope')
        samples = report.get('samples', {})
        if set(samples) != set(report.get('families', [])):
            errors.append(label + ': incomplete sample families')
        for family in ('divid', 'instrument', 'financial'):
            if family in samples and set(samples[family]) != set(report.get('codes', [])):
                errors.append(label + ': incomplete code coverage: ' + family)
        if 'market' in samples:
            if set(samples['market']) != set(report.get('periods', [])):
                errors.append(label + ': incomplete periods')
            for data in samples['market'].values():
                if set(data) != set(report.get('codes', [])):
                    errors.append(label + ': incomplete market codes')
                for code, snapshot in data.items():
                    try:
                        import pandas as pd
                        frame = pd.DataFrame(snapshot['rows'], columns=snapshot['columns'])
                        validate_market_values(frame)
                    except Exception as exc:
                        errors.append(label + ': invalid market values ' + code + ': ' + str(exc))
        if 'financial' in samples:
            for data in samples['financial'].values():
                if set(data) != set(financial_schema()):
                    errors.append(label + ': incomplete financial tables')
        _validate(samples, label, errors)

    def walk(a, b, path):
        if len(errors) >= 100:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            if set(a) != set(b):
                errors.append(path + ': keys differ')
            for key in sorted(set(a) & set(b)):
                walk(a[key], b[key], path + '/' + str(key))
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                errors.append(path + ': row/array length differs')
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, path + '/' + str(i))
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            # Counts, timestamps and book volumes must remain exact.
            if isinstance(a, int) or isinstance(b, int):
                equal = a == b
            else:
                equal = math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
            if not equal:
                errors.append(path + ': numeric value differs')
        elif a != b:
            errors.append(path + ': value differs')
    for key in ('date', 'codes', 'periods', 'families', 'samples'):
        walk(left.get(key), right.get(key), key)
    return errors[:100]


def _csv(value):
    return [v.strip() for v in value.split(',') if v.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('probe', 'sample'):
        sub = subs.add_parser(name)
        sub.add_argument('--backend', choices=['native', 'file_bridge'])
        sub.add_argument('--config', help='Explicit flat JSON configuration; no automatic discovery')
        sub.add_argument('--bridge-dir')
        sub.add_argument('--timeout', type=float)
        sub.add_argument('--output', required=True)
        if name == 'sample':
            sub.add_argument('--date', required=True)
            sub.add_argument('--codes', type=_csv, required=True)
            sub.add_argument('--periods', type=_csv, default=['1d', '1m'])
            sub.add_argument('--families', type=_csv, default=['market'])
            sub.add_argument('--sectors', type=_csv)
            sub.add_argument('--indices', type=_csv)
    sub = subs.add_parser('compare')
    sub.add_argument('left')
    sub.add_argument('right')
    sub.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        if args.command == 'compare':
            errors = compare(load_json(args.left, MAX_BYTES), load_json(args.right, MAX_BYTES))
            report = {'ok': not errors, 'errors': errors}
        else:
            config = load_config(args.config)
            args.backend = args.backend or config.get('backend', 'file_bridge')
            config['backend'] = args.backend
            config['timeout'] = args.timeout if args.timeout is not None else config.get('timeout', 5 if args.command == 'probe' else 60)
            if args.bridge_dir:
                config['bridge_dir'] = args.bridge_dir
            if args.backend == 'native':
                # Explicit CLI selection without mutating the process/node configuration.
                import importlib
                source = importlib.import_module('xtquant.xtdata')
            else:
                source = create_backend(config)
            if args.command == 'probe':
                if args.backend == 'native':
                    report = {'ok': True, 'import_only': True, 'note': 'native import is not a terminal connectivity test'}
                else:
                    data = source.probe()
                    capable = (args.backend == 'file_bridge' and data.get('protocol') == 1
                               and bool(data.get('capabilities')) and all(data['capabilities'].values())
                               and data.get('sector_tree') is True)
                    report = {'ok': capable, 'data': data}
            else:
                report = collect_sample(source, args.date, args.codes, args.periods, args.families, args.sectors, args.indices, backend=args.backend)
                report['backend'] = args.backend
                report['ok'] = not compare(report, report)
        if args.output:
            atomic_json(Path(args.output), report)
        print(json.dumps({'ok': report['ok'], 'output': args.output, 'errors': report.get('errors', [])}, ensure_ascii=True))
        return 0 if report['ok'] else 1
    except Exception as exc:
        report = {'ok': False, 'errors': [type(exc).__name__ + ': ' + str(exc)]}
        if args.output:
            atomic_json(Path(args.output), report)
        print(json.dumps(report, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
