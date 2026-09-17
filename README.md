# QMT Python Bridge

让外部 Python 程序通过大 QMT 内置 Python 获取行情和历史数据，保留外部环境中的 pandas、研究工具及存储方式。

项目提供历史数据文件桥和本机实时行情内存通道。QMT 侧负责调用数据接口，外部程序负责数据处理、校验与保存，也可以生成统一入口，让历史与实时服务共用一个 QMT 策略。

**实验性 / Alpha · 数据只读 · 不提供交易功能**

GitHub：[YangSal/qmt-python-bridge](https://github.com/YangSal/qmt-python-bridge)

发行包名：`bigqmt-data-bridge` · Python 导入名：`bigqmt_bridge`

## 主要功能

| 功能 | 说明 |
|---|---|
| 历史数据读取 | 从 QMT 缓存读取日线、1 分钟、5 分钟及 Tick，外部返回 pandas DataFrame |
| 自动 K 线下载 | 显式启用后，下载股票、ETF、指数已结束交易日的日线、1 分钟和 5 分钟数据，并校验读回结果 |
| 批量采集与续采 | 按指定证券或 QMT 板块成员建立采集计划，分片执行，保存进度、错误及文件摘要 |
| 复权事件 | 获取事件日期及原生七字段数据；K 线归档默认保持不复权 |
| 实时行情 | 指定证券订阅、最新快照轮询、退订与健康状态查询，通过本机命名管道传输 |
| 磁盘归档 | 外部消费者将历史 K 线和收到的实时快照保存为 gzip JSONL，不依赖数据库 |
| 其他只读接口 | 提供合约、板块、指数权重及财务数据的有限接口，具体可用性取决于 QMT 环境与权限 |

本项目提供部分 `xtdata` 风格接口，不是完整的 `xtquant` 替代品，也不透传任意代码或交易指令。

## 工作原理

```text
外部 Python                              大 QMT 内置 Python
数据处理、校验、归档                       受控的数据接口调用
        │                                        │
        ├── 历史：文件请求、持久任务、结果文件 ────┤
        └── 实时：本机命名管道、内存消息 ──────────┘
```

历史请求和缓存可以保存在磁盘。实时行情的桥接通信只使用内存通道；外部消费者接收后，可按需要另行归档。

自动下载任务保留任务 ID 和处理状态，支持中断后对账续采。外部请求超时不能取消已经进入 QMT 的原生调用，不应删除未确认任务后盲目重发。

## 环境要求

- Windows 与已获授权、支持内置 Python 策略的大 QMT 客户端。
- 外部 Python 3.10+，主要依赖为 pandas。
- 内置端以 Python 3.6 为兼容目标，不向 QMT 复制外部二进制模块或安装项目依赖。
- 外部客户端与 QMT 在同一台电脑运行；接口和数据权限由券商客户端管理。

## 安装

在项目根目录使用外部 Python 安装：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m bigqmt_bridge --help
```

也可以使用已有 conda 或虚拟环境，无需重复创建环境。不要在 QMT 内置 Python 中执行安装命令，也不要把外部 `site-packages` 加入内置解释器路径。

## 选择使用方式

| 使用方式 | 入口与配置 | 使用文档 |
|---|---|---|
| 读取已准备的历史缓存 | `qmt_bridge/strategy.py`、`config.example.json`，默认 `cache_only` | [缓存读取与配置](docs/cache-reading.md) |
| 自动下载历史 K 线 | `qmt_bridge/strategy_auto.py`、`config.auto.example.json`，显式启用下载 | [自动下载指南](docs/automatic-download.md) |
| 历史批量采集与复权归档 | `python -m bigqmt_bridge.market_collector`、`config.market.example.json` | [采集计划与续采](docs/market-collector.md) |
| 实时行情或历史与实时统一服务 | 通过 `bigqmt_bridge.memory_session` 生成独立运行包和 QMT 启动脚本 | [部署与使用](docs/memory-transport.md)、[行情接口](docs/market-memory.md) |

QMT 侧策略应取消“启动本地python”，编译后选择“运行”，不是“回测”。源码目录、桥接运行目录和消费者归档目录分别配置，不要让多个策略同时占用同一历史运行目录。

实时通道首次部署需按对应文档完成环境与通道检查。升级已加载的内置模块应按部署流程切换版本，修改磁盘源码不会自动更新运行中的 QMT 策略。

## Python 调用示例

准备 QMT 历史缓存并配置 `config.local.json` 后：

```python
from bigqmt_bridge.backend import create_backend
from bigqmt_bridge.config import load_config

client = create_backend(load_config('config.local.json'))
frames = client.get_market_data_ex(
    field_list=['time', 'open', 'high', 'low', 'close', 'volume', 'amount'],
    stock_list=['000001.SZ'],
    period='1d',
    start_time='20260904',  # 替换为需要读取且已准备数据的日期
    end_time='20260904',
    count=-1,
    dividend_type='none',
    subscribe=False,
    fill_data=False,
)
print(frames['000001.SZ'])
```

返回值为 `{证券代码: DataFrame}`，`time` 使用 UTC 毫秒。业务日期按北京时间处理，避免依赖机器本地时区。调用异常通过 `bigqmt_bridge.QmtDataError` 报告，不应把接口失败解释为空交易日或停牌。

更多入口见[小批量历史归档](docs/collector.md)和[接口源码](bigqmt_bridge/backend.py)。

## 功能边界

- 自动下载只支持已结束交易日的日线、1 分钟和 5 分钟 K 线，默认关闭；不包含 Tick、财务或指数权重自动下载。
- 实时采用 `poll_snapshot` 最新快照轮询，不保证捕获两次轮询之间的每次行情更新，不提供完整逐笔增量流。
- 当前实时单批请求与累计订阅均最多 10 只证券。这是本项目的实现限制，不是 QMT 官方容量声明；暂不支持全市场实时采集。
- 统一入口中的历史与实时调用串行执行，同步历史调用可能延迟实时响应，不提供硬实时延迟保证。
- 财务、完整合约等接口可能受内置依赖、字段范围及账户权限影响；不同券商环境需要分别确认兼容性。
- 不提供下单、撤单、账户管理或自动生产切换。运行目录、认证配置和原始行情应保留在本机，不上传公开仓库。

## 项目结构

```text
bigqmt_bridge/             外部客户端、采集器、命名管道服务与 CLI
qmt_bridge/               QMT 内置策略、worker、文件与内存协议
config*.example.json      配置样例
skills/                   仓库配套 Skill
experiments/              独立环境检查与验证工具
tests/                    自动化测试
docs/                     使用指南、接口说明、设计与验证记录
LICENSE                   MIT 许可证
```

开发环境安装与测试：

```powershell
python -m pip install -e ".[test]"
python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q
```

## 许可证

项目沿用原作者的 [MIT 许可证](LICENSE)，以源码形式交付。代码许可不涵盖第三方行情和券商软件；项目不附带 QMT、券商程序、账号或真实行情，也不提供权限绕过。

接口背景可参阅迅投官方[内置 Python 文档](https://dict.thinktrader.net/innerApi/start_now.html)。
