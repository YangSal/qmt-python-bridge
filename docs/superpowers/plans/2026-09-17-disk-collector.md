# 磁盘采集测试客户端 Implementation Plan

> Execute inline with superpowers:executing-plans; use an independent review before delivery.

**Goal:** 外部用户通过本项目采集历史 K 线并保存到硬盘；真实实时功能缺失时明确报告 incomplete。

**Architecture:** 复用 AutomaticBackend，不修改已加载的 QMT worker。独立 collector CLI 调用
历史下载/读取，archive 模块原子保存 gzip JSONL。实时 capability 检查不调用历史行情冒充实时。
完整实时采集依赖尚未完成的 M1b/M2，不属于已可调用的桥功能。

**Tech Stack:** 现有外部 Python 3.10.20、pandas、标准库与 pytest，无新依赖。

**Spec:** ../specs/2026-09-17-disk-collector-design.md；用户已澄清这是外部使用者采集后落盘测试。

## Constraints

- 外部测试归档允许；不改变实时 IPC 内存约束，不修改运行中 worker。
- 无数据库、交易、生产切换或自动 QMT UI 操作。
- 显式日期列表；不推断交易日；只使用已结束的北京时间日期。
- 每次采集总预算默认 300 秒，单请求有界；超时不声称取消内置调用。
- 同一输出目录固定范围、单写者；下载任务使用既有确定性 ID，unknown 不另起 ID。

## Task 1: 历史采集与落盘

Files: create bigqmt_bridge/archive.py, bigqmt_bridge/collector.py, tests/test_collector.py.

Interfaces: collect_history(source, codes, periods, dates, output_dir, max_seconds=300) -> report;
write_bars(path, frame, code, period, date) -> metadata; verify_bars(path, metadata) -> bool.

- [ ] Write tests with real AutomaticBackend + existing synthetic worker rig.
  Assert cold download produces one gzip JSONL row whose code is 000001.SZ and time is
  1767596400000; manifest stores rows=1 and native download evidence.
- [ ] Assert a warm cache stores attempted=false; corrupt saved data must fail local
  verification before any new backend call. Missing saved file may be recovered using original job.
- [ ] Assert scope mismatch, wrong day, duplicate input, write failure, busy lock and
  unknown/probe failures never produce complete. Restart after interrupted write reuses the job.
- [ ] Run `python -m pytest tests/test_collector.py -q` and record missing-feature RED.
- [ ] Implement strict scope, fixed manifest, atomic gzip writing, checksum validation,
  finite per-cell work, fail-fast errors and resume with the real history backend.
- [ ] Run the focused tests; inspect decoded saved rows and metadata.

## Task 2: CLI、实时缺口与文档

Files: extend collector.py/tests/test_collector.py; create docs/collector.md;
update README.md and skills/bigqmt-data-bridge/SKILL.md.

- [ ] Test CLI history arguments and report exit codes. Test `realtime-check` yields
  state=incomplete with no market rows or historical read calls for the current bridge.
- [ ] Implement `python -m bigqmt_bridge.collector history --config ... --codes ...
  --dates ... --periods ... --output-dir ...` and `realtime-check --output-dir ...`.
- [ ] Write QMT startup steps, saved format, resume command and evidence boundaries;
  validate documented CLI with help plus offline synthetic workflow.
- [ ] Run `python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q`
  and `git diff --check`; independently review code and fix findings.
- [ ] Run actual probe and, when the user starts QMT, small-sample history collection.
  Keep real downloads, cache hits and incomplete realtime separate in the final report.

## Status

- [x] 历史采集、原子归档、续采与严格本地预检已实现。
- [x] CLI 和 realtime-check 缺口报告、README 与独立使用说明完成。
- [x] 23 项新增测试通过；独立审查 P2 已修复并复审。
- [x] 完整回归 310 passed（46.44 秒）；git diff --check 通过。
- [x] 用户启动 QMT 后 probe 成功，两日期共 18 文件 / 1,740 行归档完成；其中本次
  7 项原生下载返回且读回成功。一次状态文件访问错误按原 ID 续采后恢复。
- [ ] 实时获取 incomplete：当前桥缺少已验收内存通道和行情 API。
- [ ] 原 datacollect 完整业务清单与全量迁移未确认/未验收。

基线初次回归 286 passed / 1 failed，失败用例单独重跑通过；未修复或掩盖未确认的
时序根因。完整证据边界见 ../../research/2026-09-17-collector-live.md。
repository Skill 只补充适用范围说明，仍不声明 auto/实时工作流覆盖或安装。
