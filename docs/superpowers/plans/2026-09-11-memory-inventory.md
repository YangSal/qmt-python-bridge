# M1a 内置 Python 通信能力清单 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可由用户在 QMT 手动执行、可由外部程序校验结果的只读通信能力清单；不将模块存在性当成共享内存验证通过。

**Architecture:** 本计划仅落实设计第 4 节的第一步，作为 M1 的第一个独立可测试交付。单文件内置脚本只导入固定标准库候选、检查固定接口是否可调用，保存无账户/行情的诊断 JSON。外部校验器拒绝错误协议、陈旧会话和畸形报告；拿到内置结果后再编写 M1b 的双向通信/同步/故障测试计划，不猜测尚未可用的底层原语。

**Tech Stack:** 内置 Python 3.6 语法、标准库；外部 Python 3.10、pytest。沿用已有 py10，不安装依赖。

**Spec:** `docs/superpowers/specs/2026-09-11-stock-etf-bridge-design.md`，特别是第 2–4、9–10 节。

## Global Constraints

- 首个交易版本面向普通 A 股和 ETF 二级市场买卖；本计划不实现或调用任何交易功能。
- QMT 界面操作由用户完成；不自动点击、停止策略或重启客户端。
- 实时行情不写独立 IPC 文件；本计划的落盘内容仅是模块/函数能力诊断，不含行情或账户。
- 不修改券商权限检查，不复制二进制模块，不安装依赖，不依赖 MiniQMT 原生服务。
- 内置脚本兼容 Python 3.6；禁止依赖 importlib、pandas、xtquant 或线程执行。
- 不修改当前运行的 `qmt_bridge/strategy_auto.py`、旧 worker、旧协议或运行目录。
- 不制作安装包；不提交 evidence、账户、配置、原始行情或运行时记录。
- 能力清单无论发现多少可用接口，最终状态始终为 `incomplete`；M1b 尚未执行，不能标为任何通道 verified。

## Files and Interfaces

- `experiments/memory_v1/strategy_inventory.py`：可整体粘贴到 QMT 的独立 ASCII 单文件入口；固定候选检查、JSON 报告、init/handlebar/stop 生命周期。
- `experiments/memory_v1/inspect_report.py`：外部 CLI，按本次明确会话标识读取并校验诊断文件，输出简明能力表。
- `experiments/memory_v1/README.md`：加载步骤、仅一次执行的预期行为、外部检查命令、测试边界。
- `tests/test_memory_inventory.py`：模块缺失/存在、危险函数不调用、文件持久化和重入行为。
- `tests/test_memory_inventory_cli.py`：协议/新鲜会话/错误报告拒绝和 CLI 行为。

固定报告协议 `memory-inventory-v1`。结构至少包含 `protocol, session_id, state, python, platform,
pid, captured_at_utc, modules, qmt_callables, stages`。`session_id` 为 32 位小写十六进制，
本次由外部生成后在 QMT 脚本中设置，源码默认空字符串且运行前必须修改，防止误读取旧报告。
`state` 固定 `incomplete`；`stages.inventory=complete`，`cross_process/synchronization/security/
load/recovery/scheduling` 均为 `not_run`。库存完成不等于所有模块导入成功。

诊断目录仅在内置脚本 `REPORT_DIR` 中指定，默认 `D:\bigqmt-data-bridge\evidence\memory-v1`；
输出 `<session_id>.json`。使用排他创建，不覆盖已有同会话文件；遇到同名文件报告明确错误，
由调用者改用新会话。对文件落盘使用 flush/fsync 并检查错误；失败时不能打印 saved。
输出文件只保存可 JSON 序列化的布尔值、数字和受限长度文本，不序列化整个模块或 ContextInfo。

### Task 1: 内置能力清单与一次性入口

**Files:** Create `experiments/memory_v1/strategy_inventory.py`, `tests/test_memory_inventory.py`.

**Interfaces:**
- `collect_capabilities(context, api, session_id) -> dict`：固定候选，无客户端输入驱动的模块导入。
- `write_report(report, report_dir) -> str`：排他创建并持久化 `<session_id>.json`。
- `init(C)`：使用脚本常量收集、落盘并打印 `QMT memory inventory saved: <path>`；失败打印明确错误并抛出。
- `handlebar(C)`、`stop(C)`：不执行任何交易/查询/循环；此脚本是一次性诊断，不要求持续运行。

候选模块固定为 `mmap, _winapi, _multiprocessing, ctypes, socket, msvcrt`。
模块报告为 `{importable: bool, error: str|null, callables: {固定名称: bool}}`。
检查函数列表：mmap 的 mmap；_winapi 的 CreateNamedPipe/ConnectNamedPipe/CreateFile/ReadFile/
WriteFile/PeekNamedPipe/SetNamedPipeHandleState/CloseHandle/WaitForSingleObject/CreateMutex/
ReleaseMutex；_multiprocessing 的 SemLock；ctypes 的 WinDLL；socket 的 socket；msvcrt 的 locking。
这些仅是候选存在性，不做支持决策。不调用任何这些原语，不创建 IPC 内核对象。
QMT 固定检查 globals 和 ContextInfo 两侧的 run_time/schedule_run/subscribe_quote/
subscribe_whole_quote/get_full_tick/get_trade_detail_data/passorder/cancel；只检查 callable。
属性获取或模块导入异常按单项记录，继续其余项，不把 False 写成缺失字段。

- [ ] **Step 1: 写行为测试。** 先以 importlib.util.find_spec 或文件存在性断言说明新入口缺失，
  然后加载真实入口执行。测试用受控 importer 模拟券商缺模块，这是对外部环境的必要替身。
  断言错误隔离、字段完整及实际环境没有任何危险函数被调用。例如：

```python
def forbidden(*args, **kwargs):
    raise AssertionError('QMT functions must not be invoked')

def test_collect_does_not_invoke_trade_functions(inventory):
    report = inventory.collect_capabilities(object(), {'passorder': forbidden}, 'a' * 32)
    assert report['state'] == 'incomplete'
    assert report['qmt_callables']['globals']['passorder'] is True
    assert report['stages']['cross_process'] == 'not_run'
```

- [ ] **Step 2: 运行 RED。** `python -m pytest tests/test_memory_inventory.py -q`；缺少文件/接口
  导致断言失败，记录测试数和失败原因。拒绝会话为空/非法、文件已存在、fsync 失败、属性读取异常。
- [ ] **Step 3: 最小实现。** 遍历固定模块映射；每项 try/except 包住 __import__ 与属性检查；
  不使用 importlib。会话严格验证后，`open(path, 'x', encoding='utf-8')` 写入 JSON、flush/fsync。
  init 仅在 write_report 成功返回后打印 saved。不要重复实现旧交易或文件队列。

```python
def handlebar(C):
    pass

def stop(C):
    pass
```

- [ ] **Step 4: GREEN + Python 3.6 语法。** 运行该测试文件，并以 `ast.parse(source,
  feature_version=(3, 6))` 检查入口；执行 init 两次验证第二次不会覆盖第一次报告。
  测试输出、文件和 JSON 结构，不靠搜索源码中某行文字验收行为。
- [ ] **Step 5: 自查与提交。** 只提交本任务两文件；记录 RED/GREEN 证据、未做真实终端验证。

### Task 2: 外部诊断校验与交付

**Files:** Create `experiments/memory_v1/inspect_report.py`, `experiments/memory_v1/README.md`,
`tests/test_memory_inventory_cli.py`; update this plan's execution status.

**Interfaces:** Consumes Task 1 JSON; produces `validate_report(report, expected_session) -> dict` and
CLI `python experiments/memory_v1/inspect_report.py --report PATH --session-id ID`。
退出码 0 仅代表能力清单有效，不代表通信通过；输出必须显式包含 `transport_verified=false` 和
`state=incomplete`。畸形数据/错误会话/读取异常退出 1，不通过错误信息泄露完整原始文件。

- [ ] **Step 1: 写 CLI RED。** 使用 Task 1 真实生成的诊断和子进程执行校验器；坏协议、
  缺字段、会话不符、伪造 verified、阶段缺失/伪造、模块布尔类型错误必须失败。例如：

```python
def test_rejects_verified_inventory(inspector, valid_report):
    valid_report['state'] = 'shared_memory_verified'
    with pytest.raises(ValueError):
        inspector.validate_report(valid_report, 'a' * 32)
```

- [ ] **Step 2: 运行 RED。** `python -m pytest tests/test_memory_inventory_cli.py -q`，记录缺少功能。
- [ ] **Step 3: 实现严格校验。** 文件上限 64 KiB，拒绝非对象、无穷/NaN JSON、重复键；会话
  必须与调用方明确给定的 ID 一致。协议、状态、阶段、固定模块和固定函数集合必须匹配，
  不接受额外数据体。pid 正整数、UTC 时间有限、python/platform 有界字符串。
  输出仅列出协议、会话、未验收状态和各模块/接口布尔结果，不复制原始错误全文。
- [ ] **Step 4: GREEN、全回归和文档。** 测试报告为本机临时合成文件；复跑
  `python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q`。
  README 明确复制入口、更改 SESSION_ID、取消本地 python、运行后可自然结束、saved 日志、
  外部 CLI 和缺模块不等于项目最终不支持。不要增加包安装步骤。
- [ ] **Step 5: 提交并交付给用户加载。** 完成任务/整分支审查后，用 `uuid.uuid4().hex` 在外部
  生成一次全新会话，只给用户这一次的具体值；不把真实报告提交。用户运行后再调用外部 CLI。

## Self-review / Scope Coverage

M1a 覆盖设计第 4 节第一步；不覆盖双向挑战、10,000 条消息、同步、对象权限、负载、恢复和
调度验证。这些是 M1b 的强制验收项，当前阶段一律 not_run，不能把本计划代码完成写成 M1
验收完成。Task 1 生成的每个字段都由 Task 2 校验；固定模式与函数集合共享导入入口常量，
避免两个实现漂移，但测试期望使用独立字面量。外部导入入口不得触发 init 或写报告。

## Execution Status

- [x] Task 1 implemented and reviewed
- [x] Task 2 implemented and reviewed
- [x] Whole-branch review and regression complete
- [x] User has run QMT inventory; report checked
- [ ] Follow-on M1b plan based on actual capabilities

2026-09-11：用户已执行单次内置清单，外部校验器退出 0，状态保持 incomplete。
实际能力及未验收项见 [脱敏实测记录](../../research/2026-09-11-memory-inventory-live.md)。
Task 2 首次开发因服务容量中断，原始 RED 日志未恢复，不能声明该步骤已有完整证据；
接续开发的时间格式修复单独记录了 RED/GREEN。任务步骤保留为原实施检查清单，
完成及审查状态以本节为准。M1b 尚未执行，M1 和交易均未验收。

最终整分支审查及一次修复复审完成，无遗留问题；完整回归命令
`python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q`
结果为 287 passed（51.26 秒）。内置实测报告在最终校验器上重新校验，退出 0。
上述里程碑完成时，代码保留在 `codex/stock-etf-trading` 功能分支，当时没有合并或推送。
后续交接已推送该功能分支；2026-09-11 用户另行授权合入 `main`，
当前集成安排与后续开发入口见 [HANDOFF.md](../../../HANDOFF.md)。
