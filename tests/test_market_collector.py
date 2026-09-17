"""Full-market consumer tests drive the real file worker in temporary roots."""
import importlib.util
import json

import pytest

from bigqmt_bridge.auto_backend import AutomaticBackend
from qmt_bridge.auto_protocol import FileLock
from qmt_bridge.auto_worker import AutomaticWorker
from test_download_jobs import Context, PumpTransport


class MarketContext(Context):
    def __init__(self):
        super().__init__()
        self.sectors = {'QMT A股原名': ['000001.SZ', '600000.SH', '000001.SZ'],
                        'QMT ETF原名': ['510300.SH'], 'QMT 指数原名': ['000300.SH'],
                        'QMT 北交所原名': ['920001.BJ']}
        self.factors = {}
        self.factor_reads = []

    def get_stock_list_in_sector(self, sector_name):
        return self.sectors[sector_name]

    def get_divid_factors(self, code):
        self.factor_reads.append(code)
        result = self.factors.get(code, {})
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def rig(tmp_path):
    context = MarketContext()
    config = {'bridge_dir': str(tmp_path / 'ipc'), 'timeout': 5,
              'poll_interval': .005, 'download_timeout': 5, 'cache_prepared': False}
    worker = AutomaticWorker(config['bridge_dir'], context,
        {'download_history_data': context.download,
         'get_sector_list': lambda root: (list(context.sectors), [])}, True)
    source = AutomaticBackend(PumpTransport(config, worker), config)
    yield context, source
    worker.close()


def market():
    assert importlib.util.find_spec('bigqmt_bridge.market_collector') is not None, 'market consumer missing'
    from bigqmt_bridge import market_collector
    return market_collector


def freeze(rig, tmp_path, codes=None, **kwargs):
    return market().create_plan(rig[1], tmp_path / 'market', ['20260813', '20260814'],
        ['1d'], codes=codes or ['000001.SZ'], provenance='explicit reviewed sample', **kwargs)


def progress(tmp_path):
    return [json.loads(p.read_text('utf-8')) for p in sorted((tmp_path / 'market/shards').glob('*/progress.json'))]


def test_freeze_exact_sector_membership_deduplicates_without_inferred_classification(rig, tmp_path):
    plan = market().create_plan(rig[1], tmp_path / 'market', ['20260813', '20260814'],
        ['1d', '1m', '5m'], sectors=list(rig[0].sectors), shard_size=2)
    assert plan['codes'] == ['000001.SZ', '000300.SH', '510300.SH', '600000.SH', '920001.BJ']
    assert plan['universe_sources'][0] == {'kind': 'qmt_sector', 'name': 'QMT A股原名',
        'members': ['000001.SZ', '600000.SH', '000001.SZ']}
    assert [len(s['codes']) for s in plan['shards']] == [2, 2, 1]
    assert plan['total_units'] == 35
    assert rig[0].downloads == []


def test_plan_is_frozen_and_cannot_be_replaced_by_new_sector_snapshot(rig, tmp_path):
    freeze(rig, tmp_path)
    original = (tmp_path / 'market/plan.json').read_bytes()
    with pytest.raises(ValueError, match='exist|frozen'):
        freeze(rig, tmp_path, ['510300.SH'])
    assert (tmp_path / 'market/plan.json').read_bytes() == original


@pytest.mark.parametrize('changes', [
    {'codes': ['../000001.SZ']}, {'codes': ['000001.HK']},
    {'sectors': ['inferred_sector'], 'codes': None}, {'shard_size': 11},
    {'provenance': ''}, {'dates': ['20990101']}, {'periods': ['tick']},
])
def test_bad_plan_rejected_without_download(rig, tmp_path, changes):
    args = dict(codes=['000001.SZ'], provenance='reviewed list', dates=['20260813'], periods=['1d'])
    args.update(changes)
    with pytest.raises(ValueError):
        market().create_plan(rig[1], tmp_path / 'market', **args)
    assert rig[0].downloads == []
    assert not (tmp_path / 'market/plan.json').exists()


def test_bounded_run_resumes_pending_units_and_locally_verifies_saved_data(rig, tmp_path):
    freeze(rig, tmp_path, ['000001.SZ', '510300.SH'])
    report = market().run_plan(rig[1], tmp_path / 'market', max_units=2)
    assert {k: report[k] for k in ('total', 'completed', 'failed', 'remaining')} == {
        'total': 6, 'completed': 2, 'failed': 0, 'remaining': 4}
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['state'] == 'complete'
    assert report['completed'] == 6 and report['remaining'] == 0
    downloads, factors, reads = len(rig[0].downloads), len(rig[0].factor_reads), len(rig[0].reads)
    assert market().run_plan(rig[1], tmp_path / 'market')['state'] == 'complete'
    assert (len(rig[0].downloads), len(rig[0].factor_reads), len(rig[0].reads)) == (downloads, factors, reads)


def test_failed_code_does_not_remove_it_or_stop_other_codes(rig, tmp_path):
    freeze(rig, tmp_path, ['000001.SZ', '510300.SH'])
    rig[0].fail_codes.add('000001.SZ')
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['state'] == 'incomplete'
    assert report['failed'] == 2 and report['completed'] == 4 and report['remaining'] == 0
    failures = [i for s in progress(tmp_path) for i in s['items'] if i['state'] == 'failed']
    assert {i['code'] for i in failures} == {'000001.SZ'}
    assert all(i['reason'] == 'history_incomplete' and i['error'] for i in failures)
    count = len(rig[0].downloads)
    market().run_plan(rig[1], tmp_path / 'market', retry_failed=True)
    assert len(rig[0].downloads) == count


def test_all_factor_events_through_cutoff_saved_without_manual_cache_gate(rig, tmp_path):
    freeze(rig, tmp_path)
    rig[0].factors['000001.SZ'] = {'20200102': [1, 2, 3, 4, 5, 6, .8],
        '20260814': [0, 0, 0, 0, 0, 0, 1], '20260901': [0, 0, 0, 0, 0, 0, 1]}
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['completed'] == 3
    payload = json.loads(next((tmp_path / 'market/shards').glob('*/factors/*.json')).read_text('utf-8'))
    assert payload['events'] == [
        {'date': '20200102', 'interest': 1, 'stockBonus': 2, 'stockGift': 3,
         'allotNum': 4, 'allotPrice': 5, 'gugai': 6, 'dr': .8},
        {'date': '20260814', 'interest': 0, 'stockBonus': 0, 'stockGift': 0,
         'allotNum': 0, 'allotPrice': 0, 'gugai': 0, 'dr': 1}]
    assert payload['returned_events'] == 3 and payload['excluded_after_cutoff'] == 1
    assert payload['state'] == 'events' and payload['cutoff'] == '20260814'


def test_empty_factor_response_is_explicit_evidence_not_fabricated_factor(rig, tmp_path):
    freeze(rig, tmp_path, ['000300.SH'])
    market().run_plan(rig[1], tmp_path / 'market')
    payload = json.loads(next((tmp_path / 'market/shards').glob('*/factors/*.json')).read_text('utf-8'))
    assert payload['state'] == 'no_events' and payload['events'] == []
    assert payload['evidence'] == 'qmt_returned_empty_event_mapping'
    assert 'not_applicable' not in str(payload)


@pytest.mark.parametrize('raw', [None, [], {'20261340': [0] * 7},
    {'20200101': [0] * 6}, {'20200101': [0, 0, 0, 0, 0, 0, True]},
    {'20200101': [0, 0, 0, 0, 0, 0, float('nan')]},
    {'20200101': [0, 0, 0, 0, 0, 0, '1']},
    {'__frame__': True, 'columns': ['interest', 'stockBonus', 'stockGift', 'allotNum',
        'allotPrice', 'gugai', 'dr'], 'index': ['20200101', '2020-01-01'], 'data': [[0]*7, [0]*7]},
])
def test_invalid_factor_data_is_failed_and_never_archived(rig, tmp_path, raw):
    freeze(rig, tmp_path)
    rig[0].factors['000001.SZ'] = raw
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['failed'] == 1 and report['completed'] == 2
    assert not list((tmp_path / 'market/shards').glob('*/factors/*.json'))


def test_plan_tamper_and_output_lock_fail_before_qmt_requests(rig, tmp_path):
    freeze(rig, tmp_path)
    root = tmp_path / 'market'
    with FileLock(root / 'market.lock'):
        with pytest.raises(OSError):
            market().run_plan(rig[1], root)
    path = root / 'plan.json'
    data = json.loads(path.read_text('utf-8'))
    data['codes'] = ['510300.SH']
    path.write_text(json.dumps(data), 'utf-8')
    with pytest.raises(ValueError, match='plan|digest'):
        market().run_plan(rig[1], root)
    assert not rig[0].downloads and not rig[0].factor_reads


def test_factor_checksum_failure_preserves_original_and_does_not_rerequest(rig, tmp_path):
    freeze(rig, tmp_path)
    market().run_plan(rig[1], tmp_path / 'market')
    path = next((tmp_path / 'market/shards').glob('*/factors/*.json'))
    path.write_bytes(b'corrupt')
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['failed'] == 1 and path.read_bytes() == b'corrupt'
    assert rig[0].factor_reads == ['000001.SZ']


def test_discovery_lists_only_real_names_and_plan_cli_validates_code_file(rig, tmp_path, monkeypatch, capsys):
    module = market()
    monkeypatch.setattr(module, 'create_backend', lambda config: rig[1])
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps(dict(rig[1].config, history_mode='auto')), 'utf-8')
    discovered = tmp_path / 'sectors.json'
    assert module.main(['discover', '--config', str(cfg), '--output', str(discovered)]) == 0
    assert set(json.loads(discovered.read_text('utf-8'))['sectors']) == {
        'QMT A股原名', 'QMT ETF原名', 'QMT 指数原名', 'QMT 北交所原名'}
    codes = tmp_path / 'codes.json'
    codes.write_text(json.dumps({'codes': ['000001.SZ'], 'provenance': 'reviewed selection'}), 'utf-8')
    assert module.main(['plan', '--config', str(cfg), '--codes-file', str(codes),
        '--dates', '20260813,20260814', '--periods', '1d', '--output-dir', str(tmp_path / 'market')]) == 0
    assert module.main(['run', '--config', str(cfg), '--output-dir', str(tmp_path / 'market'),
        '--max-units', '1']) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])['remaining'] == 2


def test_supplied_sector_snapshot_is_frozen_without_qmt_contact(rig, tmp_path, monkeypatch):
    snapshot = {'schema': 'qmt-sector-universe-v1', 'captured_at_utc': '2026-09-17T00:00:00+00:00',
        'source': {'bridge_dir': str(rig[1].transport.root)}, 'sectors': rig[0].sectors}
    def forbidden(*args):
        raise AssertionError('supplied snapshot must not contact QMT')
    monkeypatch.setattr(rig[1].transport, 'call', forbidden)
    plan = market().create_plan(rig[1], tmp_path / 'market', ['20260813', '20260814'],
        ['1d'], universe=snapshot)
    assert plan['codes'] == ['000001.SZ', '000300.SH', '510300.SH', '600000.SH', '920001.BJ']
    assert plan['snapshot'] == snapshot
    assert plan['universe_sources'][0]['name'] == 'QMT A股原名'


@pytest.mark.parametrize('changes', [{'captured_at_utc': 'ambiguous'}, {'sectors': {}},
    {'schema': 'guessed'}, {'source': {'bridge_dir': 'different runtime'}}])
def test_unverified_sector_snapshot_is_rejected(rig, tmp_path, changes):
    snapshot = {'schema': 'qmt-sector-universe-v1', 'captured_at_utc': '2026-09-17T00:00:00+00:00',
        'source': {'bridge_dir': str(rig[1].transport.root)}, 'sectors': rig[0].sectors}
    snapshot.update(changes)
    with pytest.raises(ValueError):
        market().create_plan(rig[1], tmp_path / 'market', ['20260813'], ['1d'], universe=snapshot)
    assert not (tmp_path / 'market/plan.json').exists()


def test_unknown_native_download_resume_uses_original_request_identity(rig, tmp_path, monkeypatch):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    freeze(rig, tmp_path)
    original = rig[1].transport.wait
    def uncertain(request_id, timeout=None):
        result = original(request_id, timeout)
        record = rig[1].transport._load_record(request_id)
        if record['operation'] == 'download_kline':
            raise QmtRequestTimeout(request_id)
        return result
    monkeypatch.setattr(rig[1].transport, 'wait', uncertain)
    first = market().run_plan(rig[1], tmp_path / 'market')
    assert first['failed'] == 2
    native_ids = {p.stem for p in rig[1].transport.root.joinpath('records').glob('*.json')
                  if json.loads(p.read_text('utf-8'))['operation'] == 'download_kline'}
    monkeypatch.setattr(rig[1].transport, 'wait', original)
    report = market().run_plan(rig[1], tmp_path / 'market', retry_failed=True)
    assert report['state'] == 'complete' and len(rig[0].downloads) == 2
    assert native_ids == {p.stem for p in rig[1].transport.root.joinpath('records').glob('*.json')
                  if json.loads(p.read_text('utf-8'))['operation'] == 'download_kline'}


def test_progress_callback_sees_durable_counts_after_each_attempt(rig, tmp_path):
    freeze(rig, tmp_path)
    snapshots = []
    def update(summary):
        snapshots.append(summary)
        assert json.loads((tmp_path / 'market/summary.json').read_text('utf-8')) == summary
    report = market().run_plan(rig[1], tmp_path / 'market', on_progress=update)
    assert [s['completed'] for s in snapshots] == [1, 2, 3]
    assert [s['remaining'] for s in snapshots] == [2, 1, 0]
    assert report['state'] == 'complete'


def test_explicit_runtime_change_preserves_snapshot_origin_and_new_plan_binding(rig, tmp_path):
    snapshot = {'schema': 'qmt-sector-universe-v1', 'captured_at_utc': '2026-09-17T00:00:00+00:00',
        'source': {'bridge_dir': str(tmp_path / 'old-runtime')}, 'sectors': rig[0].sectors}
    plan = market().create_plan(rig[1], tmp_path / 'market', ['20260813'], ['1d'],
        universe=snapshot, allow_source_runtime_change=True)
    assert plan['snapshot']['source']['bridge_dir'] == str(tmp_path / 'old-runtime')
    assert plan['source']['bridge_dir'] == str(rig[1].transport.root)
    assert plan['source_runtime_change_authorized'] is True


def test_budget_exhaustion_never_claims_unchecked_saved_suffix_complete(rig, tmp_path, monkeypatch):
    import time
    freeze(rig, tmp_path)
    module = market()
    module.run_plan(rig[1], tmp_path / 'market')
    next((tmp_path / 'market/shards').glob('*/factors/*.json')).write_bytes(b'corrupt')
    original = module._verify_saved
    def slow_verify(*args):
        original(*args)
        time.sleep(.15)
    monkeypatch.setattr(module, '_verify_saved', slow_verify)
    report = module.run_plan(rig[1], tmp_path / 'market', max_seconds=.1)
    assert report['state'] == 'incomplete' and report['unchecked_units'] == 2
    monkeypatch.setattr(module, '_verify_saved', original)
    report = module.run_plan(rig[1], tmp_path / 'market')
    assert report['failed'] == 1 and report['unchecked_units'] == 0


def test_resume_cursor_avoids_rechecking_prefix_before_pending_tail(rig, tmp_path, monkeypatch):
    freeze(rig, tmp_path)
    module = market()
    module.run_plan(rig[1], tmp_path / 'market', max_units=1)
    def forbid_prefix(*args):
        raise AssertionError('pending tail should run before starting another verification cycle')
    monkeypatch.setattr(module, '_verify_saved', forbid_prefix)
    report = module.run_plan(rig[1], tmp_path / 'market', max_units=1)
    assert report['completed'] == 2 and report['failed'] == 0 and report['remaining'] == 1


def test_three_failures_stop_unreachable_bridge_without_touching_pending_units(rig, tmp_path, monkeypatch):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    freeze(rig, tmp_path, ['000001.SZ', '510300.SH'])
    def no_worker(request_id, timeout=None):
        raise QmtRequestTimeout(request_id)
    monkeypatch.setattr(rig[1].transport, 'wait', no_worker)
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['stop_reason'] == 'bridge_unavailable'
    assert (report['failed'], report['completed'], report['remaining'], report['run_units']) == (3, 0, 3, 3)
    assert report['bridge_check']['state'] == 'unavailable'
    untouched = [i for shard in progress(tmp_path) for i in shard['items'] if i['code'] == '510300.SH']
    assert all(i['state'] == 'pending' for i in untouched)
    assert len(list(rig[1].downloads.root.glob('*.json'))) == 2
    assert rig[0].downloads == []


def test_three_data_failures_continue_after_live_bridge_probe(rig, tmp_path):
    freeze(rig, tmp_path, ['000001.SZ', '510300.SH'])
    rig[0].fail_codes.add('000001.SZ')
    rig[0].factors['000001.SZ'] = None
    report = market().run_plan(rig[1], tmp_path / 'market')
    assert report['stop_reason'] is None and report['bridge_check']['state'] == 'available'
    assert report['failed'] == 3 and report['completed'] == 3 and report['remaining'] == 0
    assert rig[0].factor_reads == ['000001.SZ', '510300.SH']


def test_failure_circuit_probe_respects_unit_budget_and_restores_timeout(rig, tmp_path, monkeypatch):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    freeze(rig, tmp_path, ['000001.SZ', '510300.SH'])
    def no_worker(request_id, timeout=None):
        raise QmtRequestTimeout(request_id)
    monkeypatch.setattr(rig[1].transport, 'wait', no_worker)
    original = rig[1].probe
    budgets = []
    def bounded_probe():
        budgets.append(rig[1].transport.timeout)
        return original()
    monkeypatch.setattr(rig[1], 'probe', bounded_probe)
    report = market().run_plan(rig[1], tmp_path / 'market', unit_seconds=.25, max_seconds=1)
    assert report['stop_reason'] == 'bridge_unavailable'
    assert len(budgets) == 1 and 0 < budgets[0] <= .25
    assert rig[1].transport.timeout == 5


def test_explicit_retry_repairs_earlier_failure_before_later_resume_cursor(rig, tmp_path, monkeypatch):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    freeze(rig, tmp_path, ['000001.SZ', '510300.SH'])
    original_wait = rig[1].transport.wait
    uncertain_ids = []
    def once_uncertain(request_id, timeout=None):
        result = original_wait(request_id, timeout)
        record = rig[1].transport._load_record(request_id)
        if record['operation'] == 'download_kline' and not uncertain_ids:
            uncertain_ids.append(request_id)
            raise QmtRequestTimeout(request_id)
        return result
    monkeypatch.setattr(rig[1].transport, 'wait', once_uncertain)
    first = market().run_plan(rig[1], tmp_path / 'market', max_units=4)
    assert first['next_unit'] == 4 and first['failed'] == 1 and first['remaining'] == 2
    assert len(rig[0].downloads) == 3
    monkeypatch.setattr(rig[1].transport, 'wait', original_wait)
    report = market().run_plan(rig[1], tmp_path / 'market', max_units=1, retry_failed=True)
    assert report['failed'] == 0 and report['completed'] == 4 and report['remaining'] == 2
    assert report['next_unit'] == 4 and len(rig[0].downloads) == 3
    records = [json.loads(p.read_text('utf-8')) for p in rig[1].transport.root.joinpath('records').glob('*.json')]
    native = [r for r in records if r['operation'] == 'download_kline' and
        r['args'] == {'stock_code': '000001.SZ', 'period': '1d', 'date': '20260813'}]
    assert [r['request_id'] for r in native] == uncertain_ids


def factor_item(tmp_path):
    return next(i for report in progress(tmp_path) for i in report['items'] if i['kind'] == 'factors')


def factor_records(rig):
    return [json.loads(p.read_text('utf-8')) for p in rig[1].transport.root.joinpath('records').glob('*.json')
            if json.loads(p.read_text('utf-8'))['operation'] == 'divid_factors']


@pytest.mark.parametrize('terminal', ['failed', 'expired'])
def test_explicit_factor_retry_refreshes_only_confirmed_terminal_with_prior_audit(
        rig, tmp_path, monkeypatch, terminal):
    from qmt_bridge.protocol import atomic_json
    freeze(rig, tmp_path)
    if terminal == 'failed':
        rig[0].factors['000001.SZ'] = RuntimeError('factor read unavailable')
    original_wait = rig[1].transport.wait
    def expire_factor(request_id, timeout=None):
        record = rig[1].transport._load_record(request_id)
        if record['operation'] == 'divid_factors':
            path = rig[1].transport.root / 'requests' / (request_id + '.json')
            request = json.loads(path.read_text('utf-8'))
            request['deadline'] = 1
            atomic_json(path, request)
        return original_wait(request_id, timeout)
    if terminal == 'expired':
        monkeypatch.setattr(rig[1].transport, 'wait', expire_factor)
    assert market().run_plan(rig[1], tmp_path / 'market')['failed'] == 1
    original_id = factor_item(tmp_path)['request_id']
    assert rig[1].transport.lookup(original_id)['state'] == terminal
    original_submit = rig[1].transport.submit
    def assert_audit_before_publish(operation, args, request_id=None):
        if operation == 'divid_factors' and request_id != original_id:
            item = factor_item(tmp_path)
            audit_paths = list((tmp_path / 'market/shards').glob('*/factor-attempts/*/*.json'))
            assert len(audit_paths) == 1
            audit = json.loads(audit_paths[0].read_text('utf-8'))
            assert audit['request_id'] == request_id and audit['previous_request_id'] == original_id
            assert audit['previous_state'] == terminal and audit['operation'] == 'divid_factors'
            assert audit['args'] == {'stock_code': '000001.SZ'}
            assert item['request_id'] == original_id and item['refresh_history'] == [audit]
        return original_submit(operation, args, request_id)
    monkeypatch.setattr(rig[1].transport, 'wait', original_wait)
    monkeypatch.setattr(rig[1].transport, 'submit', assert_audit_before_publish)
    rig[0].factors['000001.SZ'] = {}
    report = market().run_plan(rig[1], tmp_path / 'market', retry_failed=True)
    assert report['state'] == 'complete'
    records = factor_records(rig)
    assert len(records) == 2 and len({r['request_id'] for r in records}) == 2
    assert factor_item(tmp_path)['request_id'] == original_id
    payload = json.loads(next((tmp_path / 'market/shards').glob('*/factors/*.json')).read_text('utf-8'))
    assert payload['request_id'] != original_id and len(rig[0].downloads) == 2


@pytest.mark.parametrize('uncertain', ['pending', 'unknown'])
def test_uncertain_factor_retry_never_uses_new_request_identity(rig, tmp_path, monkeypatch, uncertain):
    from bigqmt_bridge.auto_transport import QmtRequestTimeout
    freeze(rig, tmp_path)
    original_wait = rig[1].transport.wait
    def uncertain_factor(request_id, timeout=None):
        if rig[1].transport._load_record(request_id)['operation'] == 'divid_factors':
            if uncertain == 'unknown':
                (rig[1].transport.root / 'requests' / (request_id + '.json')).unlink(missing_ok=True)
            raise QmtRequestTimeout(request_id)
        return original_wait(request_id, timeout)
    monkeypatch.setattr(rig[1].transport, 'wait', uncertain_factor)
    assert market().run_plan(rig[1], tmp_path / 'market')['failed'] == 1
    original_id = factor_item(tmp_path)['request_id']
    assert rig[1].transport.lookup(original_id)['state'] == uncertain
    market().run_plan(rig[1], tmp_path / 'market', retry_failed=True)
    assert [r['request_id'] for r in factor_records(rig)] == [original_id]
    assert not list((tmp_path / 'market/shards').glob('*/factor-attempts/*/*.json'))
    assert rig[0].factor_reads == []


def test_factor_refresh_intent_survives_crash_before_progress_or_request_publish(rig, tmp_path, monkeypatch):
    freeze(rig, tmp_path)
    rig[0].factors['000001.SZ'] = RuntimeError('unavailable')
    module = market()
    module.run_plan(rig[1], tmp_path / 'market')
    original = module.atomic_json
    def crash_after_audit(path, value):
        original(path, value)
        if 'factor-attempts' in str(path):
            raise SystemExit('simulated process crash')
    monkeypatch.setattr(module, 'atomic_json', crash_after_audit)
    with pytest.raises(SystemExit, match='simulated process crash'):
        module.run_plan(rig[1], tmp_path / 'market', retry_failed=True)
    assert len(factor_records(rig)) == 1
    audit_path = next((tmp_path / 'market/shards').glob('*/factor-attempts/*/*.json'))
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes)
    monkeypatch.setattr(module, 'atomic_json', original)
    rig[0].factors['000001.SZ'] = {}
    report = module.run_plan(rig[1], tmp_path / 'market')
    assert report['state'] == 'complete'
    assert audit_path.read_bytes() == audit_bytes
    assert factor_item(tmp_path)['refresh_history'] == [audit]
    assert {r['request_id'] for r in factor_records(rig)} == {audit['original_request_id'], audit['request_id']}


def test_factor_refresh_audit_is_append_only_and_bounded(rig, tmp_path):
    freeze(rig, tmp_path)
    rig[0].factors['000001.SZ'] = RuntimeError('unavailable')
    module = market()
    module.run_plan(rig[1], tmp_path / 'market')
    frozen = {}
    for _ in range(9):
        module.run_plan(rig[1], tmp_path / 'market', retry_failed=True)
        for path, original in frozen.items():
            assert path.read_bytes() == original
        frozen = {p: p.read_bytes() for p in (tmp_path / 'market/shards').glob('*/factor-attempts/*/*.json')}
    assert len(frozen) == 8 and len(factor_records(rig)) == 9
    item = factor_item(tmp_path)
    assert len(item['refresh_history']) == 8 and 'refresh limit' in item['error']
