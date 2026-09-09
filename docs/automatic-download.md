# 自动 K 线下载（Alpha）

本页仅适用于 `0.2.0a1` 的实验性自动模式：股票、ETF、指数的 `1d`、`1m`、`5m`，且只能请求已经结束的北京时间交易日。它不提供交易、订阅、Tick 字段补齐、财务自动下载、指数权重自动下载或生产切换承诺。

默认仍是 `history_mode=cache_only`。自动模式必须使用独立入口 `qmt_bridge/strategy_auto.py` 和独立运行目录 `D:\bigqmt-auto-runtime`，不要替换或复用正在运行的旧只读桥、`qualification_v1` 或它们的队列目录。

## 1. 外部配置

复制 [`config.auto.example.json`](../config.auto.example.json) 为本机忽略的配置文件，例如 `config.auto.local.json`。样例明确包含：

```json
{
  "backend": "file_bridge",
  "history_mode": "auto",
  "bridge_dir": "D:\\bigqmt-auto-runtime",
  "timeout": 10,
  "poll_interval": 0.1,
  "download_timeout": 120,
  "cache_prepared": false
}
```

`poll_interval=0.1` 是外部端以秒为单位检查文件结果的间隔；内置策略每秒只处理一个工单。`download_timeout=120` 是每个证券×日期单元的秒级等待预算。外部超时不能中断已经进入券商 API 的内置调用。

## 2. 在大 QMT 中首次加载独立入口

1. 启动并登录已授权的大 QMT 客户端，在策略编辑器中新建策略，将 [`qmt_bridge/strategy_auto.py`](../qmt_bridge/strategy_auto.py) 完整复制进去。
2. 核对 `PROJECT_ROOT = r'D:\bigqmt-data-bridge'` 和 `BRIDGE_DIR = r'D:\bigqmt-auto-runtime'`。运行目录必须位于源码树之外，并且不能与旧桥共用。
3. **取消勾选“启动本地python”**。内置端要求 Python 3.6 标准库环境；不要安装 pandas、xtquant 或联网依赖。
4. 下载能力是危险边界，源码默认 `ENABLE_DOWNLOADS = False`。首次只读预检保持 false；明确同意在该独立策略中调用历史下载 API 后，人工改为 `ENABLE_DOWNLOADS = True`，再编译保存并运行。
5. 看到 `QMT automatic K-line bridge ready: D:\bigqmt-auto-runtime` 后，仍需用外部命令验证。日志出现 ready 不代表下载或行情读取已通过。

服务端源码不支持热重载。新 QMT 会话首次导入新模块可生效；若当前进程已经导入过 `qmt_bridge.auto_worker`，仅停止再运行同一个策略入口不保证升级到磁盘上的新代码。应在授权的安全窗口重启客户端，或未来使用版本化模块入口；不要在活跃任务期间用“重新运行”冒充升级完成。

## 3. CLI：单日下载与本地状态

以下命令在外部 Python 3.10+ 中运行。报告只保存任务范围、请求 ID、状态、时间、校验摘要和错误，不保存原始行情行。

```powershell
python -m bigqmt_bridge download --config config.auto.local.json --codes 000001.SZ --period 1d --start 20260908 --end 20260908 --output evidence/auto-daily.json
```

只有报告聚合状态为 `verified` 且 job-level `errors` 为空时退出码才是 0；`pending`、`running`、`partial`、`incomplete`、`failed`、`unknown`、freshness/probe 错误或配置错误均返回 1，并将报告或错误写入必填的 `--output`。结构校验要求 OHLC 严格为正、量额非负，并拒绝整日 `volume` 与 `amount` 都全为零的数据；有其他成交活动时，单个零成交量分钟仍可通过。`verified` 只证明该任务范围已有的缓存结构证据通过校验，不证明跨源逐值一致、数据许可、停牌或交易日历分类、全市场容量或生产可用。合法的整日无活动数据也需由后续业务规则分类，不能由本 Alpha 自动标记为已验证。

保存输出中的 `job_id`，仅查看本地持久报告时运行：

```powershell
python -m bigqmt_bridge download-status --config config.auto.local.json --job-id <returned-id> --output evidence/auto-status.json
```

`download-status` 不创建 QMT 传输、不发 probe、不推进任务。它会同时依据 `state` 和 job-level `errors` 给出退出码：例如既有 item 仍为 `verified`，但最近一次 probe 失败或本轮逐项复核尚未完成时，状态报告保留已验证证据并记录 `errors`，命令仍返回 1，不能称为本次 fresh verification 成功。要继续一个未完成任务，使用与首次完全相同的范围，并显式传回同一 ID：

```powershell
python -m bigqmt_bridge download --config config.auto.local.json --codes 000001.SZ --period 1d --start 20260908 --end 20260908 --job-id <returned-id> --output evidence/auto-resume.json
```

同一 `job_id` 与规范化范围永久绑定。报告为 `unknown` 表示底层调用结果不确定；实现不会因此重发该单元的下载，而是先按原请求 ID 对账和复核缓存。不要换新 ID 绕过 unknown，否则会破坏“至多一次”的人工处置边界。

worker probe/协议检查失败发生在任务报告已经建立之后时，manager 会把错误写入 job-level `errors`，保留自动生成的 `job_id` 和每个 cell 的 `request_id`，并且不伪造 native attempted/failed 状态。下一次同 ID 调用在 probe 前先持久化 `download refresh in progress`，该 marker 贯穿 probe 和全部逐项复核；只有整轮结束后才清除。进程中断、并发 local status 或第一项刚刷新而其余项仍是旧证据时，marker 都会阻止 `verified` aggregate 被误报为本次成功。

## 4. 多日任务必须由调用方提供日期清单

首版没有交易日历或停牌元数据供应源，也不会用周一至周五猜测节假日。多日范围必须由调用方传入有序、唯一、覆盖首尾边界且已自行确认的交易日 CSV；结构校验本身不会证明这些日期的业务分类：

```powershell
python -m bigqmt_bridge download --config config.auto.local.json --codes 000001.SZ,510300.SH --period 5m --start 20260907 --end 20260909 --expected-dates 20260907,20260908,20260909 --output evidence/auto-multi.json
```

证券代码必须是六位代码加 `.SH`、`.SZ` 或 `.BJ`。单次上限为 10,000 个代码、366 个自然日和 20,000 个证券×交易日单元；超过时由调用方拆分。不得请求北京当天或未来日期。

## 5. Python 调用

```python
from bigqmt_bridge.backend import create_backend
from bigqmt_bridge.config import load_config
from bigqmt_bridge.downloads import QmtDownloadError

backend = create_backend(load_config(r'D:\bigqmt-data-bridge\config.auto.local.json'))
job_id = None
try:
    report = backend.download_history_data2(
        ['000001.SZ'], '1d', '20260908', '20260908',
        expected_dates=['20260908'], job_id=job_id,
    )
except QmtDownloadError as exc:
    report = exc.report  # detached snapshot，保留 job_id/request_id 后再决定何时续查

print(report['job_id'], report['state'])
status = backend.download_status(report['job_id'])  # 该方法只读本地任务报告
```

多日 Python 调用同样必须提供真实 `expected_dates`。回调只接收分离的报告快照；不能通过修改回调参数改变剩余任务范围。

## 6. 运行目录与故障边界

`records/` 保存发布队列文件之前落盘的不可变请求身份与参数记录；它的存在不证明请求已经执行或完成。`states/` 是权威状态日志，`client_jobs/` 是外部任务报告。只有 `records/`、尚无队列文件或状态的发布窗口按 `unknown` 处理。`response_repairs/` 仅存放有界的响应修复标记：它让已完成的状态重新发布为响应，**不是第二条请求队列，也不授权重放下载**。

目录权限是信任边界。不要把运行目录放进 Git、云盘同步或公开共享；监控磁盘，在确认客户端和 worker 均停止后才按明确文件制定保留/清理流程。不要递归清空活跃目录。

正式新 worker 的真实终端验收尚未完成：最近一次 GUI 预检在窗口激活阶段失败，未粘贴、运行或加载新入口，也没有发起真实下载。已有 `qualification_v1` 实验结果只能证明旧实验入口在当时样本上的可行性，不能充当 `strategy_auto.py` 的 live acceptance。本版本仍为 Alpha，不能直接接管生产。
