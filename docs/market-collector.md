# 全市场历史与复权事件归档

`python -m bigqmt_bridge.market_collector` 在外部 Python 3.10+ 运行，将已确认证券范围的
历史 K 线和复权事件保存到独立磁盘目录，不使用数据库。它复用现有自动历史 worker；
代码通过离线测试不代表全市场真实终端采集已经完成。真实运行未执行的门禁为 `incomplete`。

## 冻结范围

本轮目标是沪深北 A 股、ETF、指数；`20260813,20260814`；`1d,1m,5m`。
证券集合来自真实 QMT 板块成员快照或明确经过人工核对的代码列表。程序保留板块原名、
原成员列表与重复成员，最终证券列表去重。它不会按代码前缀推测资产分类、推测板块 ID，
也不会从“目前板块成员”推导过去某日全部已退市证券。历史时点完整成分仍需单独证据。

先复制并修改仓库的 `config.auto.example.json`，使用绝对路径并确认对应 worker 已由用户
启动。`history_mode` 必须为 `auto`。复权只读请求不需要把 `cache_prepared` 猜测设为 true。

以下命令中的 `python` 指现有外部 Python 环境；替换示例路径，不自动操作 QMT 界面：

```powershell
python -m bigqmt_bridge.market_collector discover --config D:/qmt-test/auto.local.json --output D:/qmt-test/sectors.local.json --max-seconds 30
```

`sectors.local.json` 记录 QMT 返回的真实名称。将所需原名写入另一个 JSON 数组文件，例如
本轮终端实际观察到的 `沪深京A股`、`沪深ETF`、`沪深指数`、`京市指数`；其他终端应重新发现。

```powershell
python -m bigqmt_bridge.market_collector plan --config D:/qmt-test/auto.local.json --sectors-file D:/qmt-test/selected-sectors.local.json --dates 20260813,20260814 --periods 1d,1m,5m --output-dir D:/qmt-archive/market-20260813-14
```

已有真实快照时，使用 `--universe-file` 可避免重新请求成员。格式为：

```json
{
  "schema": "qmt-sector-universe-v1",
  "captured_at_utc": "2026-09-17T00:00:00+00:00",
  "source": {"bridge_dir": "D:\\qmt-auto-runtime"},
  "sectors": {"实际QMT板块原名": ["000001.SZ", "600000.SH", "920001.BJ"]}
}
```

```powershell
python -m bigqmt_bridge.market_collector plan --config D:/qmt-test/auto.local.json --universe-file D:/qmt-test/universe.local.json --dates 20260813,20260814 --periods 1d,1m,5m --output-dir D:/qmt-archive/market-20260813-14
```

快照来源必须与配置 runtime 一致；若已明确确认是同一 QMT 终端的独立新 runtime，可在
`plan` 命令加 `--allow-source-runtime-change`。计划同时保留原来源与新运行来源，不更改
已有冻结计划。该参数是来源迁移的显式记录，不是同终端身份的自动证明。

另一种输入为 `--codes-file`，JSON 内容是 `{"codes":["000001.SZ"],"provenance":"人工核对来源及范围"}`。
程序校验沪深北代码格式；来源说明和分类正确性由提供者负责，语法合法不证明证券曾上市。
每个计划最多 20,000 个证券、31 个显式日期和三个支持周期；每分片最多 10 只证券。
`plan` 只冻结成员，不下载 K 线。同目录不能再次建计划；日期/范围/来源变更应另选目录。

## 有界执行与续采

```powershell
python -m bigqmt_bridge.market_collector run --config D:/qmt-test/auto.local.json --output-dir D:/qmt-archive/market-20260813-14 --max-seconds 300 --max-units 100 --unit-seconds 30
```

预算用完后原命令可重复运行。默认每项输出 JSON 进度；`--progress-every 10` 可降低输出频率。
摘要包含 `total/completed/failed/remaining`，恒满足总数相加关系；`remaining` 指未完成尝试，
失败单列。两个日期×三个周期加一份复权事件，共每证券 7 个单元。只有全部单元成功且
已有归档校验通过，整体 `state` 才是 `complete`；有失败或待运行项时退出码 1、`incomplete`。
另有 `unchecked_units` 表示当前遍历周期尚未处理/校验的尾部单元；即使所有文件以前保存
成功，预算中断校验时仍为 incomplete。持久游标使下次从尾部继续，完成一轮后才重新从头
校验，不让已保存的前缀耗尽每次预算。`completed` 是持久保存计数，不等于本次已检查数。

默认续采跳过已记录失败项，继续未尝试单元。使用 `--retry-failed` 才重做失败项的恢复或对账。
显式重试从“持久游标与最早失败项两者中较早的位置”开始，因此桥恢复后会先修复此前的
失败，不必等待整个市场遍历完成。反复失败项仍占用本批预算；正常向后续采时省略该选项。
本批连续三个采集尝试失败后，会做一次只读桥健康检查，最多等待 5 秒，并同时受剩余批次
预算、单元预算和配置超时限制。桥无响应或协议不匹配时，以
`stop_reason=bridge_unavailable` 停止本批；后续单元保持 pending，不为其创建下载任务。
若健康检查正常，则继续后续证券；单个数据质量错误不会触发停批。`bridge_check` 保留
最近检查结果，`budget_exhausted`/`unit_limit` 分别表示正常预算或数量上限。外层批处理循环
遇到 bridge_unavailable 应停止续批，先恢复用户控制的桥策略，再继续同一计划。
历史下载复用原 DownloadManager 任务/原生请求 ID；即使 native 返回状态 unknown，也不会
换 ID 盲目发起第二次下载。未知原生调用未被外部超时取消；本地失败和原因仍保留在清单。
复权只读请求保留原始固定 ID，崩溃后重取既有回复。只有显式 `--retry-failed`，且原始或
最近一次只读请求的持久协议记录确认 `failed`/`expired`，才允许创建一个新的 `divid_factors`
读取 ID。先保存追加式审计，再更新分片中的有界 `refresh_history`，最后发布新读取；每个
证券最多八次刷新，每次显式重试最多新增一次。审计记录保留原 ID、前次 ID/终止状态、
新 ID、操作及参数。写入审计后崩溃，续采仍复用该次已分配 ID。pending、running、unknown、
外部超时或日志读取异常均不允许换 ID。原生历史下载从不使用这条只读刷新路径。
返回成功但事件格式不合法时也保留失败，不把数据验证错误伪装为传输终止。

单项异常不会删除证券或停止后续证券。缺历史、停牌零成交、不足完整分钟网格、未上市、
退市、权限错误等均可能导致历史单元失败；程序不会凭空把失败分类成“停牌”或“无须数据”。
实际原因见该单元的 `collection.json`、下载报告与 `progress.json`。错误不会用零值行情填补。
修复可恢复的条件后再对账；不能验证的项目继续保持 incomplete。输出、IPC 和 job 目录必须分开。

## 文件与证据

```text
plan.json                                      不变计划、真实来源、日期、分片和摘要
summary.json                                   固定大小的总体进度
shards/00000/progress.json                      有界分片的逐单元状态与错误
shards/00000/factors/000001.SZ.json              事件日期和七字段、请求 ID、返回证据
shards/00000/factor-attempts/000001.SZ/0001.json  显式只读刷新意图；每证券最多八份、不覆盖
shards/00000/cells/000001.SZ/1d/20260813/
  collection.json                              单元任务/下载/缓存/归档证据
  history/1d/000001.SZ/20260813.jsonl.gz          不复权 K 线
```

每单元完成即原子持久保存该分片和固定大小摘要；不会在每项完成时重写越来越大的全市场
明细文件。总计数在内存增量维护，启动时从分片重建。独占锁阻止同目录多写者。
续采先校验已保存文件的长度/摘要；损坏文件保留并报错，缺失文件通过同任务对账恢复。
已有数据本地验证不计为 QMT 缓存命中或本次下载；缓存命中、原生下载调用与最终读回在
各单元原始报告中区分。文件、清单和 QMT 响应含行情，均应保留在私有输出目录，不提交 Git。

## 复权语义

官方内置接口 `ContextInfo.get_divid_factors(code)` 未传日期时返回全部事件；消费者使用
既有 worker 的只读 `divid_factors` 操作，绕开不适用于此次自动消费者的旧 GUI 下载断言。
它不执行新的复权下载 API，也不修改旧 Backend 的缓存门禁。
[官方行情函数文档](https://dict.thinktrader.net/innerApi/data_function.html)

每条事件必须有可解析的北京事件日期和七个有限数值字段：`interest, stockBonus,
stockGift, allotNum, allotPrice, gugai, dr`。布尔、字符串数值、NaN、缺字段、重复事件日期、
非法日期和 None 结果都失败。全部返回事件先校验，再保留截至最后目标日的全部历史事件，
例如 2020 年的事件仍保存；不只保存 20260813/14 当天事件。返回总数和截止日之后排除数分开记录。

合法空事件映射保存 `state=no_events`、空数组与真实空返回证据；不填造 `factor=1`，
不据此声称指数复权“不适用”。调用报错的指数也保留失败。本文中的事件数据不是已经计算好
的前复权或后复权价格序列，K 线仍为 `dividend_type=none`。

## 吞吐与验收边界

统一入口已完成与实时短采样重叠的历史续采测试，见[真实终端记录](research/2026-09-17-unified-collector-live.md)。
正常同时采集时可只运行[一个 QMT 策略](memory-transport.md#统一采集入口)，无需再运行下面的独立历史入口。
全市场仍需持续续采；部分指数分钟线可能未通过既有完整网格检查，必须保留 incomplete 状态和原因。

当前旧策略 1 秒轮询会成为瓶颈。每个历史单元理想暖缓存通常需要 3 个请求（probe、验证、
最终读取），冷缓存通常至少 5 个（另加下载及下载后验证），每证券再加一次复权读取。
本轮 7,872 证券据此约需 149,568～244,032 个请求，1 请求/秒时约 41.5～67.8 小时，
还未计入下载耗时、文件写入与失败等待。此为调度下界估算，不是实测完成时间。
`run_units_per_second` 是本批尝试率，包含失败；不等于下载速度或成功率。

更短的独立策略定时器需要用户加载及实际测量；不能由 10ms 配置值宣称达到 100 请求/秒，
也不能修改运行中的 worker 假定热重载有效。本消费者单次默认 300 秒并可续采，不要求
一次进程完成整个市场。真实全市场完成、实际下载比例、失败原因与终端吞吐需单独记录。

## 独立市场历史入口 v1

此入口保留供分项历史测试；使用统一入口时，不要同时对同一 runtime 启动它。

新入口为 `qmt_bridge/strategy_market_v1.py`，使用相同且未修改的 `AutomaticWorker`，
默认 runtime 为 `D:/bigqmt-market-runtime`；外部配置样例为 `config.market.example.json`。
它不替换已有 `strategy_auto.py`，也不修改或停止用户现有策略。公开源码保持
`ENABLE_DOWNLOADS=False`。需要下载时，在单独的本机脚本副本中显式改为 True，再由用户
在 QMT 新建独立策略加载。项目目录和 runtime 可在该副本修改，不必采用维护者的路径。

建议新策略名 `BRIDGE_MARKET_V1`。用户以正常运行模式启动该新策略，不能用回测模式验证
定时器。预期启动日志包含 `QMT market history v1 ready`、选定 runtime、
`requested_timer=10nMilliSecond` 和实际 downloads 开关。加载前应确认原历史任务没有正在
执行不明状态原生调用；目录隔离不能证明多个策略的 QMT 原生调用可安全并发。

[官方定时器文档](https://dict.thinktrader.net/innerApi/system_function.html) 支持
`nMilliSecond` 周期单位。此入口请求 10ms 间隔，每回调最多开始 8 次 `poll()`，已消耗
20ms 后不再开始下一次。20ms 是两次调用之间检查的软预算：已有 `poll()` 会同步调用 QMT，
无法中断慢下载或读取；因此单次回调可能超过 20ms。停止时打印回调次数、处理数、最长
回调和超预算次数，用于实际诊断；配置值不算延迟或吞吐验收证据。

该入口不使用线程、不提供实时行情或交易。QMT 接受定时器、无行情时仍调度、长调用影响、
实际吞吐及与独立内存探针混合运行的表现均须终端实测，当前不能由离线测试宣称通过。
