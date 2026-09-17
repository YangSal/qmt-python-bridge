# 外部采集测试程序：历史 K 线落盘

程序作为本项目的使用者运行：调用大 QMT 桥，获取数据，保存到独立硬盘目录供检查。
不连接数据库、不下单。本页入口实现历史 `1d`、`1m`、`5m` 采集；
实时获取使用另一个已实现的[内存通道消费者](memory-transport.md)。旧入口
`realtime-check` 仍只生成 `incomplete` 报告，不执行实时采集。

2026-09-17 已完成[两日期小样本实测](research/2026-09-17-collector-live.md)：
18 个文件、1,740 行，其中 7 项本次实际调用下载接口后读回成功，其余本次命中缓存。

这不是原 datacollect 全部业务的迁移。股票池、历史起点、Tick、财务和元数据等需求
需另行明确和验收。初次建议一只股票、一只 ETF、一个指数、一个已确认交易日。

## 1. QMT 侧

沿用[自动下载入口](automatic-download.md)，无 worker 代码升级：

1. 若 `BRIDGE_AUTO_V1` 已运行，不重复启动；外部先做有界 probe。
2. 若未加载，在 QMT 策略编辑器新建该策略，将 `qmt_bridge/strategy_auto.py` 完整粘贴进去。
3. 默认源码根 `PROJECT_ROOT = r'D:\bigqmt-data-bridge'`，运行目录
   `BRIDGE_DIR = r'D:\bigqmt-auto-runtime'`；使用其他路径时相应调整。
4. 用户已选择测试历史下载时，在编辑器内设 `ENABLE_DOWNLOADS = True`。
   取消“启动本地python”，编译后点击“运行”，不是“回测”。
5. 预期日志：`QMT automatic K-line bridge ready: D:\bigqmt-auto-runtime`。

不要重启整个 QMT、停止其他策略或热替换已加载模块。本程序未修改嵌入端，
实时和历史合用一个 QMT 策略的操作见[统一入口](memory-transport.md#统一采集入口)。
首次连通测试不能代替行情取数测试。

## 2. 外部运行

在仓库根运行，`python` 替换为已有外部 Python 3.10+ 的完整路径即可。
不必重新安装环境；依赖与现有桥一致。

```powershell
python --version
Copy-Item -LiteralPath config.auto.example.json -Destination config.collector.local.json
python -m bigqmt_bridge probe --config config.collector.local.json --timeout 5 --output evidence/collector-probe.json
python -m bigqmt_bridge.collector history --config config.collector.local.json --codes 000001.SZ,510300.SH,000300.SH --dates 20260908 --periods 1d,1m,5m --output-dir D:/qmt-collector-test/sample-20260908
```

示例日期是既有终端证据使用过的历史交易日，**不是今天，也不保证触发实际下载**。
需要下载新日期时显式更改日期和输出目录。多日使用 `--dates 20260907,20260908`，
日期必须已确认是交易日、严格升序且不重复；程序不会通过周一至周五推断交易日，
也不自动将空结果解释为停牌或节假日。单次最多 10 个代码、31 个日期、三个周期。

配置必须显式包含 `history_mode: "auto"`、`backend: "file_bridge"` 和正确的
`bridge_dir`。`cache_prepared` 可以保持 false；由自动后端检查缓存并按需下载。
每个证券×周期×日期复用原持久下载任务；即使换输出目录，也不以新 ID 盲目重发。
保留原运行目录和 `client_jobs`，不要删除 unknown 任务来强制重新下载。

默认 `--max-seconds 300`。超出预算或任一单元失败即停止，保留前面已保存的文件。
外部等待受预算限制，但操作系统文件 I/O 和进入 QMT 的原生调用不具备硬取消保证。

## 3. 文件格式与结果

```text
sample-20260908/
  collection.json
  collector.lock
  history/1d/000001.SZ/20260908.jsonl.gz
  history/1m/000001.SZ/20260908.jsonl.gz
  history/5m/000001.SZ/20260908.jsonl.gz
  ...
```

gzip 文件解压后每行一条 JSON，包含 `code, period, date, time, open, high, low,
close, volume, amount`。`time` 保留整数 UTC 毫秒，`date` 为北京时间交易日；
价格不复权，量额保留桥返回的单位，不静默换算。分钟线校验采用现有桥的完整网格规则，
部分缺失或整日无成交会得到 incomplete，不保证适用于全部停牌/特殊品种。

```python
import pandas as pd

data = pd.read_json(
    r'D:\qmt-collector-test\sample-20260908\history\1m\000001.SZ\20260908.jsonl.gz',
    lines=True, compression='gzip', convert_dates=False,
)
data['beijing_time'] = pd.to_datetime(data['time'], unit='ms', utc=True).dt.tz_convert('Asia/Shanghai')
print(data.head())
```

`collection.json` 包含范围、源运行目录、每单元的持久下载报告、文件摘要/字节数/行数、
错误、状态及 UTC 时间。退出码 0 表示全部所选历史文件保存且校验成功，1 表示失败或
incomplete，命令参数错误由 argparse 返回 2。stdout 只打印摘要，不输出行情正文。

- `download.items[].attempted=false`：缓存校验通过，没有尝试提交下载。
- `attempted=true`：持久报告记录下载提交意图；**该标志本身不证明原生下载实际成功**。
  实际下载证据还需核对原请求 `request_id` 的 worker 状态、调用返回与读回结果。
- `action=local_verified`：本次仅核对已归档文件，不联系 QMT，不代表新鲜缓存复核。
- `state=complete`：仅代表本次明确范围的历史归档完成，不是跨源对照或生产迁移验收。

## 4. 续采与故障

原命令原参数重跑即可；同一输出目录固定绑定代码、周期、日期及桥/任务目录。
更改范围请选新输出目录。目录单写者，锁忙时明确失败，不抢占其他进程。

存在且摘要正确的文件直接复用；缺失文件按原下载 ID 对账、读回并重建；摘要不符的文件
保留原样并报错，不静默覆盖。写文件使用临时文件、flush/fsync、原子替换。
中断后 `running` 不代表成功，重跑会恢复；完整性检查不等于磁盘硬件的绝对持久保证。

健康检查超时：检查 QMT 策略状态、路径及其日志。不要连续发送更多任务，不清理活跃锁。
外部超时不取消内置调用。磁盘写失败：处理空间/权限问题后复用原命令续采。
配置或范围错误不会覆盖旧 `collection.json`，以本次退出码和 stdout 错误为准。

## 5. 实时功能测试状态

```powershell
python -m bigqmt_bridge.collector realtime-check --output-dir D:/qmt-collector-test/realtime
```

当前稳定返回退出码 1，报告 `state=incomplete`、`transport_verified=false`、
`received_messages=0`。这是旧子命令的占位声明，不是整个项目的能力声明。
使用 `bigqmt_bridge.realtime_collector` 执行真正的快照获取和归档测试；
先按[内存通道流程](memory-transport.md)完成当前终端的 M1b 验证。
用户本次已明确归档是外部消费者行为；该用途不将磁盘文件变为实时桥接通道。

历史数据、实时用户归档、配置、日志与测试证据不要提交到 Git。
现有 repository Skill 仍只覆盖 legacy/cache_only；本页为采集器独立使用说明。
