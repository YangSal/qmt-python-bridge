# 缓存读取与文件桥配置

本页说明 `cache_only` 历史文件桥的安装、缓存读取、配置及对照工具。
自动下载见[自动下载指南](automatic-download.md)，实时与统一入口见[部署说明](memory-transport.md)。
以下命令从项目根目录执行；本页的文件协议和信任边界不适用于实时内存通道。

## 快速开始（cache_only）

本节是兼容的旧只读模式：使用 `qmt_bridge/strategy.py`、`config.example.json` 和人工准备缓存。它不会自动下载历史数据。需要自动 K 线时请使用[独立自动下载入口](automatic-download.md)。

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
2. 在编辑器中新建普通 Python 策略，打开或粘贴 [`qmt_bridge/strategy.py`](../qmt_bridge/strategy.py) 的完整内容。各券商菜单名称可能不同。
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

财务契约在 [`bigqmt_bridge/schemas/financial.json`](../bigqmt_bridge/schemas/financial.json)，包含 Balance、Income、CashFlow、Pershareindex、Capital、Holdernum、Top10holder、Top10flowholder 八表请求字段。它是客户端的严格合同，不是券商 API 支持声明。不要通过删掉缺失字段来掩盖兼容问题。

## 接口范围

外部对象提供以下有限的 xtdata 风格方法，详细签名见 [`backend.py`](../bigqmt_bridge/backend.py)：

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

在默认 cache_only 模式中，最后三个方法只是兼容确认入口，不执行下载；在 auto 模式中只有 `download_history_data2` 会执行受限 K 线下载，并另提供本地只读的 `download_status(job_id)`。财务与指数权重自动下载明确拒绝。本页文件桥后端不提供 `get_full_tick`、实时订阅回调、交易接口，也不支持把任意 xtquant 调用原样透传。外部 `get_local_data(data_dir=...)` 不能选择内置端缓存路径，会明确报错。

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
