# 开发交接：QMT Python Bridge

更新日期：2026-09-11。本文是后续开发入口，不是生产上线或交易验收声明。

## 1. 项目归属与工作目录

- 开源仓库：[YangSal/qmt-python-bridge](https://github.com/YangSal/qmt-python-bridge)。
- 本机唯一开发目录：`D:\bigqmt-data-bridge`。后续代码、测试、计划、文档均在这里维护。
- 本次交接分支：`codex/stock-etf-trading`；交接前代码里程碑为 `a1f5f27`。
  此次更新推送功能分支，不合并 `main`。接手时用 Git 核对当前分支、远端和工作区，
  不将本文的历史提交号当成永远有效的最新版本。
- 不再回到原私人数据采集项目开发桥接，不复制其配置、数据库凭据、代码历史或生产调度。
  本仓库应能独立阅读、测试和维护；接手者不需要原聊天记录或被忽略的开发临时记录。
- 保留 MIT 许可证及原版权声明。代码许可不包含 QMT 软件、行情再分发或账户操作授权。
- 只交付源码、测试、文档和 Skill；不制作安装包，不自动创建 Release 或发布 PyPI。

## 2. 已确认目标和不可变边界

外部量化策略、依赖、研究与数据库继续运行在自己的 Python 环境；大 QMT 内置 Python
仅承担受控数据和交易调用。两端部署在同一台 Windows 电脑，不依赖 MiniQMT 原生服务。

1. 首个交易范围：普通 A 股与 ETF 二级市场，先做限价买卖；不含信用、期货、期权、
   可转债、ETF 申赎、新股申购或资金划转。
2. 实时行情只走内存，不能回退到独立行情 IPC 文件、内存盘或普通文件映射。
   历史缓存、订单账本、去重记录和不含行情的诊断摘要可以持久保存。
3. QMT GUI 操作全部由用户完成。需要加载、停止策略或重启时，给准确步骤和预期日志；
   不自动点击，也不默认重跑策略能刷新内置模块缓存。本机命令、Python 和离线测试可自行执行。
4. 当前默认只读。交易设计获批不等于任意模拟订单授权；实际测试前逐次确认账号性质、
   账户、证券、方向、数量、价格。没有实盘启用或生产采集切换授权。
5. 不安装缺失的内置二进制模块、不修改券商限制、不依赖后台线程安全的猜测。
6. 未来同一 QMT 进程内，交易就绪与历史下载先采用互斥模式；旧 worker 尚无此门禁，
   两个不同运行目录不能证明安全并行。
7. 等待必须有界：单次诊断默认最多 30 秒，完整探针最多 5 分钟；超时及时报告。
   外部超时不表示已取消内部 QMT 调用。

## 3. 已完成、已验证与未完成

| 项目 | 当前证据 | 不能扩大解释为 |
|---|---|---|
| legacy/cache_only 文件桥 | 已有协议、读取、规范化及离线测试 | 全部数据类别可用或自动下载 |
| auto K 线下载 | 独立入口，显式 opt-in；股票/ETF/指数的 1d/1m/5m | Tick、财务或全市场生产采集已迁移 |
| 正式 auto 入口实测 | 2026-09-11 验证 000001.SZ、510300.SH、000300.SH 的 20260908 数据；9 组读回，3 组 5m 实际下载，其余命中缓存 | 9 组均发生下载、全历史或完整终端验收 |
| M1a 内置能力清单 | 独立脚本、严格外部校验、离线与用户终端诊断完成 | 已建立共享内存或命名管道连接 |
| 交易与实时行情 | 已批准设计，尚无正式客户端或闭环验收 | 已能买卖、撤单、订阅或替代完整 xtquant |

交接前完整回归为 **287 passed**，代码经过逐任务与整分支审查。
后续修改必须重跑测试，不能直接沿用该数字宣称当前通过。
初次 M1a 校验器开发曾因服务容量中断，原始 RED 日志未恢复；后续修复有独立 RED/GREEN，
不要补造历史测试证据。

### 内置环境实际清单

实测 Python 3.6.8：`mmap.mmap` 可调用；`_winapi` 管道相关检查项可调用，
但 `CreateMutex`、`ReleaseMutex` 缺失；`_multiprocessing`、`ctypes`、`socket`
不可导入。`msvcrt.locking` 存在，但文件锁不能直接充当本项目无文件同步方案。

`ContextInfo` 上存在定时、快照和订阅入口；全局存在查询、`passorder`、`cancel`。
本次只检查存在性，没有调用这些功能。报告始终 `state=incomplete`、
`transport_verified=false`；除 inventory 外六项阶段均为 `not_run`。

## 4. 接手后先读什么

1. [AGENTS.md](AGENTS.md)：本仓库开发与安全约定。
2. [README.md](README.md)：当前可用功能、模式与命令。
3. [股票/ETF 桥接设计](docs/superpowers/specs/2026-09-11-stock-etf-bridge-design.md)：
   已批准的 M1–M4 合同，是未来交易实现的依据。
4. [M1a 实施计划](docs/superpowers/plans/2026-09-11-memory-inventory.md)与
   [能力清单实测](docs/research/2026-09-11-memory-inventory-live.md)：从完成状态继续，不重做诊断。
5. [自动下载说明](docs/automatic-download.md)与
   [正式入口实测](docs/research/2026-09-11-auto-worker-live.md)：保留既有历史功能和证据边界。

早期 `docs/design.md`、`docs/implementation-plan.md` 属于历史文件桥方案，
不能覆盖新设计对实时内存通信和交易安全的要求。
现有 [Skill](skills/bigqmt-data-bridge/SKILL.md) 仍只覆盖 legacy/cache_only；
尚不适用于 auto、M1a 或未来交易。后续需独立更新与验证，不能仅改宣传文字。

## 5. 代码地图与本机运行线索

| 路径 | 职责 |
|---|---|
| `bigqmt_bridge/` | 外部客户端、CLI、规范化、自动下载任务 |
| `qmt_bridge/strategy.py`、`worker.py` | 旧 cache_only 内置入口及文件 worker |
| `qmt_bridge/strategy_auto.py`、`auto_worker.py` | opt-in 自动 K 线入口及持久任务处理 |
| `experiments/memory_v1/` | M1a 一次性能力清单与外部校验器，非正式内存传输 |
| `experiments/qualification_v1/` | 早期独立 P0/P1 实验，与正式 auto 入口证据分开 |
| `tests/` | 离线回归；完整命令还需显式包含 qualification 测试 |
| `evidence/`、`*.local.json` | 私有运行证据与配置，Git 忽略，不上传 |

本机最后一次用户确认的历史策略名为 `BRIDGE_AUTO_V1`，入口 `strategy_auto.py`，
运行目录为 `D:\bigqmt-auto-runtime`；这不是对接手时仍在运行的保证，先做有界健康检查。
不停止它来做新探针，不热改已加载模块。公开源码的 `ENABLE_DOWNLOADS=False` 保持安全默认。

用户已运行 `MEMORY_INV` 一次性诊断，私有报告位于 `evidence\memory-v1`。
交接文档不包含真实会话编号或原始报告；现有脱敏记录足够继续设计。
如确需重测，外部生成新会话，按 [实验说明](experiments/memory_v1/README.md) 请求用户加载；
不能覆盖旧报告，保存后自然结束是正常行为。

## 6. 下一步：先完成 M1b，不直接写下单包装器

**首先在本仓库编写 M1b 实施计划**，基于实际内置能力和官方接口/Windows 资料确定方案，
再按测试驱动开发与独立审查执行。不要重复询问已确认的同机部署、交易范围和 GUI 边界。

M1b 必须覆盖：

- 命名共享内存优先，但必须有可靠跨进程同步证据；缺失时验证本机命名管道备选。
  不以 GIL、整数写入、随机名称或校验和代替同步和身份认证。
- 双向挑战、协议/会话不匹配、重复序号、长度上限、截断损坏、队列满载。
- 至少 10,000 条合成消息逐条核对，按有界批次处理，不能长时间阻塞 QMT 回调。
- 内核对象权限与跨用户隔离，非阻塞/异步处理以及资源关闭。
- 无行情时的控制请求调度，往返和调度间隔 p50/p95/p99/max；不先承诺毫秒级。
- 用户配合停止/重启独立探针的恢复测试、旧会话拒绝；不重启整个 QMT 或影响其他策略。

只有必需项全部通过才能输出 `shared_memory_verified` 或 `named_pipe_verified`；
缺证据为 `incomplete`，确定无法支持才是 `unsupported`。没有已验收通道就不进入 M2。

随后按原设计顺序推进：

1. M2：内存行情、健康/失效状态、模拟账户及委托成交只读查询。
2. M3：限价买卖、撤单、回报、持久去重、未知状态对账、双端门禁和历史下载互斥。
3. M4：断连重启、混合负载、一个交易日的模拟稳定性观测、迁移文档及 Skill。

未知委托不得盲目重发；API 返回不等于柜台成交。实际模拟订单仍需单独确认参数。
历史全量、Tick、财务和生产采集迁移另有验收缺口，不因 M1 完成而自动关闭。

## 7. 开发验证和发布

使用已配置的外部 Python 3.10+（可沿用现有 conda 环境，无需重复安装），先确认解释器。
内置端保持 Python 3.6 语法与受限依赖；外部语法检查不能替代内置运行验证。

```powershell
Set-Location D:\bigqmt-data-bridge
git status --short
git branch --show-current
git remote -v
python --version
python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q
git diff --check
```

直接 `pytest` 默认只收集 `tests/`，不要和上述完整命令的数量混淆。
离线测试不代表自动允许执行真实 QMT 下载、账户查询或订单操作。

发布前审查 `git diff --cached` 和待推送提交：只提交明确文件；不公开账号、凭据、
真实行情、日志、报告、券商二进制或私人项目内容。新证据只提交脱敏摘要。
提交说明不含助手产品名称。推送前核对 remote，禁止强推；合并 `main` 单独决定。
每次接手和交付都更新本文的阶段状态、证据链接与下一步，不把尚未实现功能写成支持。
