# QMT Python Bridge

让外部 Python 读取大 QMT 内置 Python 暴露的数据接口：历史走文件桥，实时实验走本机内存通道。

GitHub：[YangSal/qmt-python-bridge](https://github.com/YangSal/qmt-python-bridge)。Python 发行包名为 `bigqmt-data-bridge`，导入名为 `bigqmt_bridge`。

当前新增的 [M1a 能力清单](experiments/memory_v1/README.md)
已完成诊断。新增[命名管道验证与行情消费者](docs/memory-transport.md)，每个终端需单独
完成合成、故障和用户重启门禁；不能把能力检查或离线测试当作终端验收。

新增[外部采集测试程序](docs/collector.md)：使用本桥自动下载/读取历史 K 线，按证券、
周期、日期保存 gzip JSONL 到独立硬盘目录，支持文件完整性检查与断点续采，不入数据库。
全市场及复权事件使用[分片续采程序](docs/market-collector.md)。新实时消费者为
`python -m bigqmt_bridge.realtime_collector`；旧 `realtime-check` 仍只是能力缺口提示。
内存行情支持指定证券订阅与最新快照轮询，逐笔增量完整性和交易尚不提供。
当前 10 只限制是首轮验证的实现边界，非 QMT 官方容量声明；实际终端只完成 2 只短采样。
需要同时测试历史和实时时，可生成[统一 QMT 入口](docs/memory-transport.md#统一采集入口)，
QMT 只运行一个采集策略，外部消费者分别落盘。

2026-09-17 的[统一入口终端实测](docs/research/2026-09-17-unified-collector-live.md)：
内置 Python 3.6.8 的内存通道及手动停止/恢复通过；历史采集同时运行时，股票和 ETF
的 60 秒实时采样收到 118 组快照、236 条新鲜行情，源时间更新 39 次，归档校验和退订通过。
已冻结两日期、7,872 证券、55,104 单元的全市场历史/复权任务并恢复续采，**全量尚未完成**；
部分指数分钟线未通过完整网格校验，保留为 incomplete，不冒充下载成功。
后续长采集发现 Windows 拒绝替换进度文件会使外部进程退出；外部消费者现对
PermissionError 增加最多 0.5 秒重试，永久失败仍报错，不重复提交 QMT 下载任务。
采集 CLI、资格测试程序和自动化测试源码均随仓库提供；实际行情、认证配置和运行日志不随仓库发布。

**实验性 / Alpha · 数据只读 · 不下单 · 自动下载仅限已结束交易日 K 线且须显式 opt-in**

`0.2.0a1` 新增独立的自动模式，可下载并严格校验股票、ETF、指数的 `1d`、`1m`、`5m` K 线；默认模式仍为 `history_mode=cache_only`，不会下载。正式入口已在 2026-09-11 完成[单日小样本验证](docs/research/2026-09-11-auto-worker-live.md)：三证券×三周期读回通过，其中三组 5 分钟实际触发下载，日线和 1 分钟命中缓存。自动模式不含交易、订阅、Tick、财务自动下载或生产切换，也尚未完成全面真实终端验收。2026-09-09 的[独立 P0/P1 实验](docs/research/2026-09-09-p0-p1-results.md)属于更早的 `qualification_v1`，其证据与正式入口分开记录。

本项目从一个已有数据采集项目中提取。目标是保留外部 Python 的 pandas、研究和存储环境，让内置 Python 只承担有限的数据读取。它不是完整的 `xtquant` 替代品，也不保证任意券商版本、账号权限和数据种类均可用。

> 可以开源代码，不等于可以转授权行情。请自行确认券商、迅投及数据提供方的接口使用和数据再分发许可。本项目不附带 QMT、xtquant、券商源码、账号或真实行情样本，不提供权限绕过。

## 目录

- [适用范围和当前状态](#适用范围和当前状态)
- [工作原理](#工作原理)
- [快速开始（cache_only）](#快速开始cache_only)
- [自动 K 线下载（opt-in）](#自动-k-线下载opt-in)
- [Python 调用](#python-调用)
- [只读采样和对照](#只读采样和对照)
- [配置说明](#配置说明)
- [接口范围](#接口范围)
- [故障排查](#故障排查)
- [安全、性能和生产切换](#安全性能和生产切换)
- [开发与发布](#开发与发布)

## 适用范围和当前状态

适合已经获得大 QMT 使用权限、希望在外部 Python 读取小规模历史样本并验证迁移的开发者。不适合直接接管实盘交易、全市场高频订阅，或未经验证替换整条生产采集流水线。

| 能力 | 实现情况 | 尚需验证 / 限制 |
|---|---|---|
| 文件协议、锁、超时、结果完整性 | 已实现并有离线测试 | 不等于真实客户端稳定性验收 |
| cache_only 日线 / 1分钟 / 5分钟 / Tick | 优先 `C.get_market_data_ex_ori`，外部构造 DataFrame | 新原始行情路径需要真实券商数据对照；缓存必须预先准备 |
| auto 日线 / 1分钟 / 5分钟 | 独立 worker 对股票、ETF、指数逐单元下载并严格读回校验 | 仅已结束交易日；必须显式 opt-in；正式入口单日小样本通过，完整终端/生产验收仍待完成 |
| 复权因子 | 支持事件日期、七字段规范化 | 历史样本有返回记录，仍需跨端逐值对照 |
| 简版合约 | 可调用并保留返回字段 | 部分客户端公开封装只有约 30 个键，不能冒充完整合约 |
| 板块树 / 成员 | 通过全局板块树接口及 ContextInfo 取成员 | 大 QMT 显示名称不等于原生分类 ID，不能按名字猜主键映射 |
| 指数权重 | 先按人工确认的成分板块取全体成员，再逐批查询 | 需验证成员集合和权重单位；合计近 100 只是粗检查 |
| 财务八表 | 已有字段契约和验证逻辑 | 部分内置封装仍要求 pandas；不保证可运行或完整 |
| `download_*` 接口 | cache_only 仅检查人工缓存确认；auto 的 `download_history_data2` 执行持久任务 | auto 仅在 `state=verified` 且 job-level `errors` 为空时成功；不含 Tick、财务、权重下载 |
| 原生 xtquant 基线 | CLI 可选，使用本地缓存接口 | 必须有仍可连接的授权原生环境；导入成功不代表连通 |
| 指定证券内存行情 | 统一或独立入口；订阅/退订、最新快照轮询、健康状态、外部用户归档；两证券真实短采样通过 | 最多10只；每终端须先通过 M1b；不保证逐笔增量完整性，不开放全推 |
| 交易、任意代码执行 | 不提供 | 没有 `XtQuantTrader`、下单、撤单或任意 RPC |

已有离线测试使用合成数据和模拟 ContextInfo。即使全部测试通过，也不能据此宣称财务、完整合约或全市场 Tick 已具备生产接管能力。正式接入前应在自己的券商客户端重做验收。

## 工作原理

```text
外部 Python 3.10+                         大 QMT 内置 Python 3.6
bigqmt_bridge                            qmt_bridge.strategy / strategy_auto
   │  写请求 JSON                             │
   ├──────── 本机独立 IPC 目录 ────────────────┤
   │                                  定时回调依入口每 1/2 秒处理一个请求
   │                                  调用白名单 ContextInfo 接口
   │  读结果 manifest + gzip JSON             │
   └─ 校验 UUID / 协议 / 长度 / SHA256 ───────┘
      在外部规范化为 pandas DataFrame
```

内置端自身只导入 Python 标准库；但它调用的券商封装可能自行导入 pandas。优先使用原始行情方法只解决相应行情封装的依赖问题，不会自动解决财务封装的 pandas 依赖。

cache_only 请求只允许 `probe`、`market_data`、`divid_factors`、`financial`、`instrument`、`sectors`、`sector_stocks`、`weights`。auto 入口另允许受限的 `download_kline`，每个命令只能处理一只证券、一天和一种 `1d`/`1m`/`5m` 周期。普通客户端不需要手工操作协议文件。

## 快速开始（cache_only）

本节是兼容的旧只读模式：使用 `qmt_bridge/strategy.py`、`config.example.json` 和人工准备缓存。它不会自动下载历史数据。需要自动 K 线时不要修改这套入口，请跳到下一节。

以下命令为 Windows PowerShell。`python` 应指向你选择的 **外部 Python 3.10+**，不是内置 Python。

### 1. 准备目录和外部环境

将源码解压或 clone 到 `D:\bigqmt-data-bridge`。如果使用其他路径，修改后续命令和策略内 `PROJECT_ROOT`。

```powershell
Set-Location D:\bigqmt-data-bridge
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m bigqmt_bridge --help
Copy-Item -LiteralPath config.example.json -Destination config.local.json
```

后文使用 `.\.venv\Scripts\python.exe`。如果你使用现有 conda 环境，可以替换为该环境解释器的完整路径，无需另建 venv。

外部依赖为 pandas；pytest 只用于开发测试。不要求安装 Redis、ZMQ、数据库驱动或外部 xtquant。仅在生成原生基线时需要你自己已有的 xtquant 环境。当前发行包声明的 Python 版本范围只针对外部客户端。

### 2. 在大 QMT 中加载服务端

1. 启动并登录已授权的大 QMT 客户端，进入支持内置 Python 的策略编辑环境。
2. 在编辑器中新建普通 Python 策略，打开或粘贴 [`qmt_bridge/strategy.py`](qmt_bridge/strategy.py) 的完整内容。各券商菜单名称可能不同。
3. 核对脚本开头两个路径：

   ```python
   PROJECT_ROOT = r'D:\bigqmt-data-bridge'
   BRIDGE_DIR = r'D:\bigqmt-data-bridge-runtime'
   ```

   `PROJECT_ROOT` 是包含 `qmt_bridge` 子目录的源码根目录；`BRIDGE_DIR` 是独立运行目录，不是源码目录，也不要指向其他桥正在使用的目录。

4. **取消勾选编辑器右上角的“启动本地python”**，点击“编译”保存，再点击“运行”（不是“回测”）。该选项开启时按普通脚本执行，不会触发 `init` / `handlebar`，本策略可能只显示“开始运行→结束运行”。参见迅投官方[独立 Python 进程说明](https://dict.thinktrader.net/innerApi/interface_operation.html#独立python进程)。脚本通过 `C.run_time('bridge_poll', '2nSecond', ...)` 注册回调，`handlebar` 不承担取数工作；不用历史回测结果证明桥在线。
5. 确认日志出现 `QMT data bridge ready: ...`。还需要下一步外部 probe 验证定时器实际工作。

策略文件使用 ASCII，便于兼容需要 GBK 的编辑器。路径若包含中文，需自行保证编辑器保存编码与文件编码声明一致；建议初次测试使用纯英文路径。

**不要**在内置 Python 中运行 `pip install .`，也不要把外部 Python 的 site-packages 加入其路径。内置端只加载源码根目录里的 `qmt_bridge` 包。离线测试检查了 Python 3.6 语法，但不保证每个券商环境都提供同一套 API。

升级服务端时，先确认没有活跃客户端请求，再停止该桥策略、更新文件并重新启动。修改 Python 文件不会自动让已运行的策略加载新实现。不要为测试随意停止已有生产桥。

部分内置环境缺少 `importlib`。策略现在允许在这种环境中正常首次加载，并提示 `module reload unavailable`，这不是启动失败；仍须看到 `QMT data bridge ready` 并通过外部 probe。缺少该模块时，停止再启动策略可能仍使用 Python 缓存中的旧 worker：后续升级已加载的服务端代码，需要安排安全窗口重启 QMT 客户端。不要自动重启客户端或混入外部 Python 标准库；其他依赖错误仍会原样抛出。

### 3. 验证连接

`config.local.json` 的 `bridge_dir` 必须与脚本中的 `BRIDGE_DIR` 一致。运行：

```powershell
.\.venv\Scripts\python.exe -m bigqmt_bridge probe --config config.local.json --timeout 5 --output evidence\probe.json
```

标准输出给出简短结果，完整探测保存在 `evidence\probe.json`。成功退出码为 0，失败为 1；参数格式错误通常为 2。

本版本 `probe` 报告包含 `worker_version: 2`、`market_reader`、Python 版本、方法是否 callable、板块树接口是否存在。`ok: true` 表示探测条件通过，**不表示这些方法已经成功返回真实数据**。缺少某个被探测的方法也会让总体 `ok` 为 false，需查看详细字段，不要一律当作文件连接失败。

本项目并不要求绑定账号 ID；登录和接口权限由客户端管理。

### 4. 准备缓存并取一个小样本

先在 QMT 的数据管理界面准备指定品种、日期和周期的历史缓存，核对下载完成且内容新鲜。仅在确认后，将 `config.local.json` 中：

```json
"cache_prepared": true
```

该标志是人工确认，不是缓存检测器，不会触发下载；维护期间不能用它让失败“变成功”。

以下使用固定北京时间交易日作为示例，请改成两端都已准备的真实日期：

```powershell
.\.venv\Scripts\python.exe -m bigqmt_bridge sample --config config.local.json --date 20260904 --codes 000001.SZ --periods 1d --families market --output evidence\bridge-daily.json
```

样本文件包含采样范围、字段、逐行数据、错误和每类耗时。它不连接数据库、不下单，也不会调用历史下载接口。

## 自动 K 线下载（opt-in）

自动模式使用独立入口 `qmt_bridge/strategy_auto.py`、样例 `config.auto.example.json` 和独立运行目录 `D:\bigqmt-auto-runtime`。必须人工将独立策略内的 `ENABLE_DOWNLOADS` 从默认 false 改为 true，并保持“启动本地python”未勾选。单日 CLI 示例：

```powershell
python -m bigqmt_bridge download --config config.auto.local.json --codes 000001.SZ --period 1d --start 20260908 --end 20260908 --output evidence/auto-daily.json
python -m bigqmt_bridge download-status --config config.auto.local.json --job-id <returned-id> --output evidence/auto-status.json
```

完整的加载、同 ID 续查、unknown 不重发、多日交易日历和 Python 调用说明见[自动 K 线下载指南](docs/automatic-download.md)。正式入口已有单日小样本实测，完整终端及生产验收仍未完成。

任务的 item `state` 保留既有下载/校验证据；本次 probe 或协议 freshness 失败记录在 job-level `errors`，因此调用方必须同时检查 `state=verified` 和 `errors=[]`。新一轮调用会在 probe 前持久化 `download refresh in progress`，并让该 marker 贯穿全部逐项读回；只有整轮结束后才清除，不会在 probe 刚成功或部分 item 刚刷新时把旧 aggregate 当成新鲜成功。

## Python 调用

以下代码在源码根目录运行；安装后也可以在其他目录运行，但应传配置文件的实际绝对路径。

```python
from bigqmt_bridge.backend import create_backend
from bigqmt_bridge.config import load_config

xtdata = create_backend(load_config(r'D:\bigqmt-data-bridge\config.local.json'))
frames = xtdata.get_market_data_ex(
    field_list=['time', 'open', 'high', 'low', 'close', 'volume', 'amount'],
    stock_list=['000001.SZ'],
    period='1d',
    start_time='20260904',
    end_time='20260904',
    count=-1,
    dividend_type='none',
    subscribe=False,
    fill_data=False,
)
print(frames['000001.SZ'])
```

`frames` 是 `{代码: DataFrame}`，`time` 为 UTC 毫秒。转换北京时间使用：

```python
import pandas as pd
frame = frames['000001.SZ']
beijing_time = pd.to_datetime(frame['time'], unit='ms', utc=True).dt.tz_convert('Asia/Shanghai')
```

不要依赖机器时区、`datetime.now()` 的默认日期或 `time.localtime()`。12 位的早期毫秒时间戳和 13 位毫秒时间戳均有专门处理。

异常类型为 `bigqmt_bridge.QmtDataError`。调用者应记录失败并保留证据，不应把桥错误当成“停牌”“空交易日”或“板块已删除”。本项目没有数据库写入层，也不会替调用方回滚已经写入的批次。

## 只读采样和对照

### 获取原生基线

在仍可用的原生 QMT / MiniQMT 授权环境中，安装本项目外部客户端，然后用该环境的 Python 执行：

```powershell
python -m bigqmt_bridge sample --backend native --date 20260904 --codes 000001.SZ --periods 1d --families market --output evidence\native-daily.json
```

原生取样使用 `xtdata.get_local_data`；本工具不补下载。确保原生端也已具备同日缓存。可以在另一台机器生成基线后复制到本机受控的 `evidence` 目录。

原生 `probe` 只测试能否导入 xtquant，报告含 `import_only: true`，不是客户端连通性验证。大 QMT 已登录不代表原生服务同时可连接；不要为了取得基线擅自切换生产客户端的登录模式。

### 执行对比

```powershell
.\.venv\Scripts\python.exe -m bigqmt_bridge compare evidence\native-daily.json evidence\bridge-daily.json --output evidence\daily-diff.json
```

比较范围、日期、代码、周期、字段、行数与每个值。整数精确比较，浮点相对/绝对容差均为 `1e-9`；数组次序保留，重复行不会被丢弃，最多记录 100 个错误。不同 scope 的样本不能验收通过。

`compare` 不验证样本来源身份：复制同一文件两次也可能相等。跨端验收还需保留两端实际生成命令、项目/解释器/客户端版本以及原始报告；没有独立原生来源记录时，只能称为文件内容相等，不能称为迁移验收通过。

空对象、空帧、缺码、缺列、错误报告都会使验收失败，即使两边都为空。无复权事件的合法空样本需要另选有效样本或人工说明，不代表所有空值都是数据源故障。不得为通过比较而只取字段交集、猜单位、静默重标权重或改写源数据。

已返回的核心行情字段（time、OHLC、lastPrice、volume、amount）必须是非布尔的有限数值，不能以 null、NaN、Infinity 或字符串冒充有效价格/量额。自动 K 线结构校验还要求 OHLC 严格为正，并拒绝整日 `volume` 与 `amount` 都全为零的数据；日内存在其他活动时，单个零成交量分钟仍可保留。这不等于停牌或交易日历检测，合法的整日无活动数据需由后续业务规则分类。此规则仅用于行情，不把财务字段中的所有空值一律视作错误。

### 其他样本

在上述 `sample` 命令上替换这些参数；未验收接口可能明确失败：

| 数据 | 参数 | 先决条件 |
|---|---|---|
| 分钟线 | `--periods 1m --families market` | 对应日分钟缓存 |
| Tick | `--periods tick --families market` | 近期 tick 缓存和全部严格字段；先单股单日 |
| 复权事件 | `--families divid` | 历史复权数据；从 1990 到指定日期取样 |
| 财务八表 | `--families financial` | 内置依赖、字段、历史版本均需验证 |
| 完整合约 | `--families instrument` | `instrument_fields` 来自完整原生基线 |
| 板块 | `--families sectors --sectors 沪深A股` | 树根与成员身份明确 |
| 指数权重 | `--families weights --indices 000300.SH` | 已验证的 `index_sectors` 映射 |

`--codes` 最多 10 个唯一代码。CLI 行情样本逐股请求；Python 客户端 K 线可每批最多 10 股，Tick 每批仍仅 1 股。

## 配置说明

配置是扁平 JSON，不包含顶层 `qmt` 键，不读取原采集项目配置，也不自动读取环境变量。未知键会报错，避免拼写错误静默生效。

| 键 | 缺省行为 | 说明 |
|---|---|---|
| `backend` | `file_bridge` | `native` 仅供 CLI 基线；`create_backend` 只创建文件桥 |
| `bridge_dir` | 无，文件桥必填 | 独立 IPC 目录；相对路径相对于配置文件目录解析 |
| `cache_prepared` | 未确认，取行情/复权/财务失败 | 只能是 JSON true/false；不是自动下载或新鲜度检测 |
| `timeout` | probe 5 秒、sample 60 秒 | 每次请求等待上限，不能硬中断内置调用 |
| `poll_interval` | 0.1 秒 | 外部检查响应间隔，不改变 cache_only/auto 服务端各自 2/1 秒调度周期 |
| `history_mode` | `cache_only` | `auto` 才创建自动 K 线后端；自动 CLI 在命令作用域显式设为 auto |
| `download_timeout` | 120 秒 | auto 模式每个证券×日期单元的有限等待预算 |
| `job_dir` | `<bridge_dir>/client_jobs` | auto 模式持久任务报告目录；相对路径按配置文件定位 |
| `batch_size` | 10 | 整数 1～10；Tick 固定 1 |
| `sector_root` | 空字符串 | 部分客户端需要真实根节点名称 |
| `instrument_fields` | 空列表 | 完整合约的必需键全集，必须来自可靠原生基线 |
| `index_sectors` | 空对象 | 指数代码到已验证成分板块的映射，不自动猜中文名 |

命令行 `--backend`、`--bridge-dir`、`--timeout` 优先于配置；配置优先于默认值。命令行 `--bridge-dir` 的相对路径相对于当前工作目录，建议使用绝对路径。未指定 `--config` 时不自动寻找 `config.local.json`。

财务契约在 [`bigqmt_bridge/schemas/financial.json`](bigqmt_bridge/schemas/financial.json)，包含 Balance、Income、CashFlow、Pershareindex、Capital、Holdernum、Top10holder、Top10flowholder 八表请求字段。它是客户端的严格合同，不是券商 API 支持声明。不要通过删掉缺失字段来掩盖兼容问题。

## 接口范围

外部对象提供以下有限的 xtdata 风格方法，详细签名见 [`backend.py`](bigqmt_bridge/backend.py)：

```text
probe()
get_market_data_ex(...), get_local_data(...)
get_divid_factors(stock_code, start_time='', end_time='')
get_instrument_detail(stock_code, iscomplete=False)
get_sector_list(), get_stock_list_in_sector(sector_name)
get_index_weight(index_code)
get_financial_data(stock_list, table_list=None, ...)
download_history_data2(...), download_financial_data2(...), download_index_weight()
```

在默认 cache_only 模式中，最后三个方法只是兼容确认入口，不执行下载；在 auto 模式中只有 `download_history_data2` 会执行受限 K 线下载，并另提供本地只读的 `download_status(job_id)`。财务与指数权重自动下载明确拒绝。此版本不提供 `get_full_tick`、实时订阅回调、交易接口，也不支持把任意 xtquant 调用原样透传。外部 `get_local_data(data_dir=...)` 不能选择内置端缓存路径，会明确报错。

## 故障排查

| 现象 | 检查与处理 |
|---|---|
| `QMT bridge timeout` | 对比两端目录、ACL、策略日志、定时器是否运行、GUI 是否被阻塞；停止自动重试后再调查 |
| 只有“开始运行→结束运行”，没有 ready | 检查“启动本地python”是否勾选；本桥需要取消勾选后编译保存，再运行，并检查“日志输出” |
| `No module named qmt_bridge` | `PROJECT_ROOT` 必须指向包含该包的源码根目录；不是包自身目录 |
| 第 4 行 `No module named importlib` | 旧版策略将重载模块作为硬依赖。重新打开本项目最新 `qmt_bridge/strategy.py` 并完整替换编辑器中的旧内容；只修改磁盘文件不一定会更新 QMT 已保存的策略副本。不需要 `pip install importlib` |
| `No module named pandas` 出现在内置端 | 确认报错接口及 `market_reader`；原始行情可能绕过该依赖，财务封装仍可能需要它；不能混装外部 Python 库 |
| `cache_prepared` 错误 | 人工确认实际缓存后再设置；不能为绕过错误直接改 true |
| `empty QMT cache` / 缺字段 | 核对代码、周期、日期、下载、权限及维护状态；空不是验收成功 |
| `complete instrument contract requires...` | 从原生完整基线整理字段全集；简版返回不能用来定义“完整” |
| 板块列表有名称但不匹配原生分类 | 核对分类提供商、层级、稳定身份和成员，不按同名直接映射 |
| `index_sectors must map...` | 先验证指数成分全集，再配置；单股权重能返回不说明全集正确 |
| worker 锁被占用 | 不删活跃锁、不抢占；查明持有者，确认无活跃请求后按授权停止旧策略并重启 |
| 原生 `无法连接xtquant服务` | 登录大 QMT 不代表原生服务在线；从仍获授权的原生节点取基线 |
| 响应超出上限 | 缩小日期窗口或标的批量；当前无自动大窗口拆分 |

维护、服务器空返回和接口结构性差异必须区分。等待维护结束可解决暂时性问题，但不能修复缺依赖、固定字段投影或分类命名差异。

## 安全、性能和生产切换

- **信任边界是本机目录权限。** SHA256 用于损坏检测，不是签名或身份认证。目录可写者能伪造数据；不要把 IPC 目录开放给不可信用户或放到公开共享、Git仓库同步和云盘同步目录中。
- 请求 UUID 和 deadline、协议版本、长度等被检查；结果数据先发布、manifest 后发布。坏请求隔离，进程锁避免多个 worker 同时消费；崩溃后的只读请求可能重放，因此不要扩展为交易通道而沿用此语义。
- 请求 JSON 上限 1 MiB，单响应未压缩 JSON 上限 64 MiB；这些不是完整内存占用上限，大返回值在序列化前仍可能占据较多内存。
- cache_only 入口每 2 秒处理一个工单；其中 5000 股 Tick 仅调度下限约 2.8 小时，还没计读缓存和序列化。auto 入口每秒处理一个工单，但每个证券×日期单元仍串行下载和验证。两种模式都应先测小批量容量，不要直接调大生产超时掩盖瓶颈。
- 外部超时只结束外部等待，不能硬中断 QMT 内置 API。不要持续堆积新请求；恢复可能需要用户停止策略或重启客户端。
- 工单、结果及异常证据暂不自动清理。监控磁盘，制定保留期；仅在确认消费者和 worker 停止后清理已消费且不再需要的明确文件，勿递归清空运行目录。
- 真实结果可能含受许可限制的数据和运行路径，`evidence/` 默认忽略，不上传公开仓库或公开 issue。

生产切换应是独立工作：逐通道只读对照、至少连续多个交易日验收、缓存自动供应与容量测试通过后，再安排停旧调度/启新调度。不要同时运行两个写入相同目标的采集器，不要在活跃采集目录热替换代码。保留回退条件，但原生权限已取消时不能承诺能回退。

## 开发与发布

### 运行离线测试

```powershell
Set-Location D:\bigqmt-data-bridge
.\.venv\Scripts\python.exe -m pytest tests/ experiments/qualification_v1/test_qualification.py -q
```

测试使用临时目录和合成数据，不连接真实 QMT。覆盖协议往返、损坏/超时/过期、锁、字段完整性、Tick 大整数和盘口、财务身份、采样对比、独立配置与 Python 3.6 语法。这里的 Python 3.6 检查是语法检查，不是完整的 Python 3.6 运行时认证。

### 项目结构

```text
bigqmt_bridge/          外部客户端、CLI、规范化、JSON字段契约
qmt_bridge/            内置端策略、worker、文件协议（标准库）
experiments/           独立验证工具，不代表正式传输或交易能力
tests/                 离线回归测试
docs/                  拆分设计、实施记录、验证与发布清单
config.example.json    legacy/cache_only 样例，缓存确认缺省为 false
config.auto.example.json  auto 独立运行目录样例，下载能力仍需服务端 opt-in
pyproject.toml         外部客户端安装元数据
LICENSE                MIT
```

### 源码交付

后续维护以 GitHub 源码为交付物，不再构建或发布安装包。保留 README、tests、docs 和字段资源，按上方命令验证源码；已有构建记录仅属于历史验证，不代表后续源码已经打包。不要把运行目录、真实样本、`dist/` 或 `build/` 上传。

### 准备 GitHub 发布

先按 [`docs/release-checklist.md`](docs/release-checklist.md) 审查本地文件。新建独立仓库，不继承私人原项目历史。提交前查看 `git status` 和暂存差异，确保没有样本、日志或本地配置。

已有仓库使用核对过的 remote 地址推送经过审查的功能分支；合并主分支应单独决定。当前版本为 `0.2.0a1 / Alpha`，在仓库说明中保留限制和验收状态；不构建或发布安装包，不创建 GitHub Release 或 PyPI 发布。

### 来源和许可证

本项目沿用原作者 MIT 许可证，版权声明见 [LICENSE](LICENSE)。代码许可不涵盖第三方行情和券商软件。无需第三方桥接库，未捆绑第三方 RPC 实现。

接口背景可参阅迅投官方 [内置 Python 快速开始](https://dict.thinktrader.net/innerApi/start_now.html)、[数据接口](https://dict.thinktrader.net/innerApi/data_function.html) 和 [使用须知](https://dict.thinktrader.net/innerApi/user_attention.html)。实际支持范围以你使用的券商客户端、授权和实测为准；本项目不据公众号文章声明统一停用日期或通用迁移政策。
