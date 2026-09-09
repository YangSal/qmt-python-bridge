# Automatic K-line Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 正式支持外部 K 线自动下载、持久状态、读回验证和未知结果续查，保留旧只读模式。

**Architecture:** 独立 auto-kline-v1 内置 worker 和文件传输；外部任务编排与严格校验；兼容 facade 及显式 CLI。底层结果复用已有 gzip/manifest，不修改旧 worker 或 qualification 实验。

**Tech Stack:** Python 3.10+ / pandas / pytest；内置 Python 3.6 标准库。

**Spec:** [../specs/2026-09-09-automatic-kline-design.md](../specs/2026-09-09-automatic-kline-design.md)

## Global Constraints

- 外部 Python 3.10+；内置 Python 3.6 标准库，无网络、线程、xtquant、pandas 导入。
- 本轮仅股票/ETF/指数的 1d、1m、5m 已结束交易日 K 线自动下载；无交易、订阅、Tick 字段补齐、财务自动下载或生产切换。
- 保留原 `qmt_bridge/strategy.py`、旧 worker/协议及默认只读模式；新入口 `qmt_bridge/strategy_auto.py`，运行目录 `D:\bigqmt-auto-runtime`。不修改运行中的 qualification_v1。
- 工作在用户指定独立项目的新分支；保留既有未提交修改，不公开证据/账户/配置，不推送。
- 用 apply_patch 编辑；只提交任务自身文件，commit 说明不出现 codex/chatgpt 等字样。不操作真实终端、不下载真实行情，现场验收由主任务负责。

---

### Task 1: 版本化请求、持久去重与内置执行端

**Files:** Create `qmt_bridge/auto_protocol.py`, `qmt_bridge/auto_worker.py`, `qmt_bridge/strategy_auto.py`, `bigqmt_bridge/auto_transport.py`, `tests/test_auto_protocol.py`, `tests/test_auto_worker.py`.

**Interfaces:**
- `auto_protocol.PROTOCOL = 'auto-kline-v1'`; `OPERATIONS = old OPERATIONS | {'download_kline'}`.
- `request_hash(operation, args) -> str`; `FileLock(path)` context manager / close, nonblocking OS lock (Windows msvcrt, Unix fcntl), create parent directories. `validate_download(args) -> (stock_code, period, date)` follows spec exactly.
- `AutomaticWorker(root, context, api, downloads_enabled=False)`, `.poll() -> bool`, `.close()`. Probe extends old dispatch result with `automatic_kline`, `downloads_enabled`, `worker_version=3`.
- `AutomaticTransport(config)` uses bridge_dir/timeout/poll_interval. `.submit(operation, args, request_id=None) -> str`; `.lookup(request_id) -> dict`; `.wait(request_id, timeout=None) -> dict`; `.call(operation,args) -> data` (returned only). `QmtRequestTimeout(QmtDataError)` exposes request_id; lookup statuses pending/unknown/returned/failed/expired. Envelope also includes request_id,args_hash,data,error; native call's `late` field retained.
- `download_kline` response data `{api, data_ready:False, return_value, elapsed_seconds}`. No global download function → failed, not native fallback.

- [ ] **RED:** Add tests before source; test successful real file roundtrip, repeat same ID once, different args rejected, max 32 pending, invalid dates/code/period, disabled download, interrupted worker unknown/no replay, completed response still returned after restart, immutable record but no queued request returns unknown, late native return remains queryable, lock excludes second worker.

```python
def test_disabled_download_never_reaches_api(tmp_path):
    from qmt_bridge.auto_worker import AutomaticWorker
    from bigqmt_bridge.auto_transport import AutomaticTransport
    calls = []
    worker = AutomaticWorker(str(tmp_path), object(), {'download_history_data': lambda *a: calls.append(a)})
    transport = AutomaticTransport({'bridge_dir': str(tmp_path), 'timeout': .1})
    rid = transport.submit('download_kline', {'stock_code':'000001.SZ','period':'1d','date':'20260908'})
    try:
        worker.poll()
        assert transport.lookup(rid)['state'] == 'failed'
        assert calls == []
    finally:
        worker.close()
```

- [ ] Run `python -m pytest tests/test_auto_protocol.py tests/test_auto_worker.py -q`, capture expected missing implementation failures.
- [ ] Implement canonical immutable records before queue publication; validate paths/ID/limits; one-request native dispatch plus explicit download enable gate. On startup running files become unknown, never requeued. Reuse old dispatch for existing read-only operations and old result serialization, new envelope validates protocol and identity.
- [ ] Implement external bounded queue publisher with OS lock, stable-ID duplicate handling and lookup/wait/call; unknown/timeouts never publish a second request. Keep server journals authoritative, fail closed on corruption.
- [ ] Strategy uses PROJECT_ROOT plus independent root and `ENABLE_DOWNLOADS=False` opt-in. init closes prior worker then registers run_time at 1nSecond; no startup downloads, no importlib dependency; handlebar pass, stop closes lock. Document no hot-reload promise in entry comments.
- [ ] Run focused tests, Python 3.6 AST check for new qmt files, then full tests once; self-review and commit only six task files: `feat: add durable QMT download transport and worker`.

### Task 2: 外部任务编排、严格校验与 facade

**Files:** Create `bigqmt_bridge/kline.py`, `bigqmt_bridge/downloads.py`, `bigqmt_bridge/auto_backend.py`, `tests/test_download_jobs.py`, `tests/test_auto_backend.py`. Modify `bigqmt_bridge/backend.py`, `bigqmt_bridge/config.py`.

**Interfaces:**
- `validate_kline(raw, stock_code, period, date) -> dict` returns rows/first_beijing/last_beijing; require explicit original UTC integer time, no stime fallback, exact standard grids and OHLCV per spec.
- `DownloadManager(transport, config).download(stock_list, period, start_time, end_time, expected_dates=None, job_id=None, callback=None) -> report`; `.status(job_id) -> report` only local read. `QmtDownloadError(QmtDataError)` carries `.report`.
- Report contains `job_id`, normalized `request`, `state`, `items` (code/date/request_id/state/error/validation), totals and timestamps. States pending/running/awaiting_data/verified/incomplete/failed/unknown; aggregate success only all verified, partial if some verified and other known failures, unknown if unresolved invocation. Use atomic JSON, job OS lock, deterministic IDs for jobs and per-item downloads; never store full rows in report.
- `AutomaticBackend(InnerBackend)` selected by `create_backend(history_mode='auto')`. It uses AutomaticTransport; download_history_data2 delegates manager and exposes download_status. Preserve cached legacy APIs by narrowly adding `_history_cache()` to parent (default calls `_cache()`), used only by market reads; auto override disables manual history-cache gate. Financial and index-weight downloads in auto mode explicitly raise; Tick read explicitly rejects. Enforce raw UTC K-line payloads in auto market-read path without duplicating parent normalization body.
- Config new keys: history_mode (`cache_only` default / `auto`), download_timeout (finite positive, default 120), job_dir (path, default bridge_dir/client_jobs). Existing options/behavior unchanged. Relative job_dir resolves relative to explicit config file like bridge_dir.

- [ ] **RED:** Add end-to-end tests using Task 1's real file transport and fake ContextInfo cache. Assert cold cache completes, warm cache avoids download, status survives new manager, same parameters produce same job ID, wrong explicit ID parameters reject, unknown state reads/reconciles but never redownloads, incomplete data doesn't succeed, failed cells recorded, unknown cell stops new submissions, partial count correct. Cover 2 codes × 2 provided dates, cap validation before any publish.

```python
def test_old_worker_is_rejected_before_download():
    from bigqmt_bridge.downloads import DownloadManager
    from bigqmt_bridge import QmtDataError
    import pytest
    class Old:
        def call(self, op, args):
            assert op == 'probe'
            return {'worker_version':2}
    with pytest.raises(QmtDataError):
        DownloadManager(Old(), {}).download(['000001.SZ'], '1d', '20260908', '20260908')
```

- [ ] Run `python -m pytest tests/test_download_jobs.py tests/test_auto_backend.py -q`, record failures.
- [ ] Implement source-time validator: DataFrame construction never repairs missing original numeric time. For date grid use UTC conversion to Asia/Shanghai. Reject duplicates, missing rows, non-finite/negative amount-volume, impossible OHLC. Don't compare minute sums to daily exactly.
- [ ] Implement manager according to spec, normalize/validate scope first, probe new version before download, read cache first, one stable native request if required, repeat only read queries within bounded deadline, atomically save every state transition. Catch transport failure with request identity and keep uncertain outcome; no transparent native retry. Resume reuses stored scope/IDs, revalidates verified cache, only reconciles attempted items.
- [ ] Add narrow facade/config integration and tests proving default cache_only behavior unchanged; auto works with cache_prepared=false and rejects unsupported kwargs/old worker explicitly.
- [ ] Run focused tests plus full suite, self-review and commit only this task's files: `feat: add verified automatic K-line download jobs`.

### Task 3: CLI、部署文档与打包验证

**Files:** Modify `bigqmt_bridge/cli.py`, `README.md`, `pyproject.toml`, `docs/validation.md`; create `config.auto.example.json`, `docs/automatic-download.md`, `tests/test_auto_cli.py`.

**Interfaces:** Keep existing probe/sample/compare untouched in default behavior. Add `download` with --config, --bridge-dir, --codes, --period (1d/1m/5m), --start, --end, --expected-dates CSV, --job-id, --output(required); force explicit history_mode=auto (config or CLI command scope), refuse native. Add `download-status` with --config, --bridge-dir, --job-id, --output(required), no QMT requests. CLI returns 0 only verified download, else 1 with persisted report/error and IDs. No raw market data in reports.

- [ ] **RED:** CLI tests call main against tmp_path, fake only slow external manager boundary when needed; assert output artifact, exit codes, ID-preserving errors, local-only status, and legacy CLI behavior.
- [ ] Run `python -m pytest tests/test_auto_cli.py -q`, record intended failures.
- [ ] Add handlers before legacy parser paths; emit QmtDownloadError.report rather than losing it in generic exception. Resolve explicit config consistently.

```powershell
python -m bigqmt_bridge download --config config.auto.example.json --codes 000001.SZ --period 1d --start 20260908 --end 20260908 --output evidence/auto-daily.json
python -m bigqmt_bridge download-status --config config.auto.example.json --job-id <returned-id> --output evidence/auto-status.json
```

- [ ] Write sample JSON with backend=file_bridge, history_mode=auto, bridge_dir=D:\\bigqmt-auto-runtime, timeout=10, poll_interval=0.1, download_timeout=120, cache_prepared=false. Document ENABLE_DOWNLOADS opt-in, unchecked local-Python box, separate entry, exact CLI/Python commands, multi-day calendar requirement, same-ID resume, unknown-state no resend, seconds-level polling, no production readiness promise. Keep old quickstart clearly labelled cache_only. Skill remains legacy-only this release; add prominent README warning that skill does not yet cover auto workflow instead of silently teaching old download semantics.
- [ ] Change package version to 0.2.0a1 and description to experimental embedded QMT file bridge with verified K-line downloads; do not publish. New modules included by existing package rules, no experiment/evidence in wheel.
- [ ] Full tests, build wheel with `python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist`, CLI help and isolated wheel import/CLI help check, inspect archive names for evidence/config.local/credentials. Record evidence honestly; formal live test pending until new entry actually loaded.
- [ ] Self-review and commit only task-owned changes: `feat: expose automatic download CLI and deployment guide`. Controller handles final broad review and live handoff.
