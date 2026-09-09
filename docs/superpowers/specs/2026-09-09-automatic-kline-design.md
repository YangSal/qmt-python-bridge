# 正式 K 线自动下载设计

状态：实现、离线回归与代码审查已完成；正式新入口的模拟 QMT 验收仍 pending。目标是外部环境自动取数，不把业务搬入 QMT。

## 范围

- 外部 Python 3.10+；内置 Python 3.6 标准库，无网络、线程、xtquant、pandas 导入。
- 本轮仅股票/ETF/指数的 1d、1m、5m 已结束交易日 K 线自动下载；无交易、订阅、Tick 字段补齐、财务自动下载或生产切换。
- 保留原 `qmt_bridge/strategy.py`、旧 worker/协议及默认只读模式；新入口 `qmt_bridge/strategy_auto.py`，运行目录 `D:\bigqmt-auto-runtime`。不修改运行中的 qualification_v1。
- 工作在用户指定独立项目的新分支；保留既有未提交修改，不公开证据/账户/配置。后续按用户新增授权，在审查验证后推送当前功能分支到指定开源仓库，不自动合并主分支，不再构建安装包。

## 接口与数据流

`history_mode=cache_only` 为兼容默认值；`history_mode=auto` 使用 AutomaticBackend/AutomaticTransport，连接新版 worker。新 worker 的 probe 明确报告 `automatic_kline=auto-kline-v1` 和 downloads_enabled；旧 worker 不得被误认为支持自动下载。

外部兼容接口 `download_history_data2(stock_list, period, start_time, end_time, *, expected_dates=None, job_id=None, callback=None)` 返回持久任务报告；未全部 verified 或本次预检失败时抛带 report 属性的 QmtDownloadError（QmtDataError 子类）。报告已创建后的预检失败必须保留生成的任务/请求 ID，在任务级 `errors` 中记录错误，不重写既有单元证据。提供 `download_status(job_id)` 只读查询及同参数/ID 续查；成功判据为 `state=verified` 且 `errors` 为空，旧报告缺少 errors 视为空列表。原调用方忽略成功返回值仍可工作。

日期为显式 YYYYMMDD。单日任务默认 expected_dates=[该日]；多日任务必须提供已有交易日清单，清单有序唯一、落在闭区间且包含请求边界。首版不建设日历供应源、不以周一至周五猜节假日。证券去重排序；上限 10,000 个代码、366 个自然日、20,000 个证券×交易日单元，超限提交前报错，调用方分批。

每个下载命令严格限一只证券、一天、一种周期；六位代码加 .SH/.SZ/.BJ。日期须早于 UTC 时钟推导的北京今天，不使用系统本地日期。所有读回保持 subscribe=False、fill_data=False、dividend_type=none。

自动模式不需要 cache_prepared；缓存只读、财务和其他旧接口的限制仍保留。自动模式的财务/权重下载明确不支持，不能假成功。自动模式的 Tick 读取拒绝并解释尚未完成字段合同。metadata 可复用旧 dispatch，但不扩大方法白名单。

## 请求协议与恢复

新增应用协议 `auto-kline-v1`，请求字段 protocol/request_id/operation/args/deadline。底层继续复用旧 gzip+manifest 完整性容器（manifest protocol=1），其中结果信封为 `{auto_protocol, request_id, args_hash, state, data, error}`。state 为 returned/failed/expired/unknown；returned 只表示接口已返回，不代表行情完整。

新白名单为原只读 OPERATIONS 加 download_kline。download_kline args 为 `{stock_code, period, date}`。内置全局 download_history_data/down_history_data 返回 False 或负数为 failed，None/True 仅返回调用信息及 data_ready=false。

运行目录含 requests/running/responses/records/states/client_jobs。发布者在 OS 文件锁内检查最多 32 个未完成队列文件、原子保存不可变的请求身份与参数记录 records/<id>.json，再发布 requests/<id>.json。record 存在不证明请求已经执行或完成，运行状态只以 states 为准。同一 ID/同一 operation+args 仅查询，不重发；同 ID 不同参数拒绝。args_hash 使用排序键的 canonical JSON SHA256，不包含 deadline。

Worker 使用独占 OS 锁，每次 poll 只处理一个请求。调用前先保存 running 状态；中断的 running 请求重启后记 unknown，不重放。records 已落盘但请求未发布也不能自动重新提交，归为 unknown；这是保守的“可能未执行”窗口，不能把 record 称为终态证据。完成、失败、过期状态持久保存；状态损坏显式失败，不能删状态重新执行。调用期间超过 deadline 但正常返回仍记 returned 并保留 late 标志，不把调用结果丢掉。

外部 submit/lookup/wait 分离，超时异常携带 request_id，不能包装成无 ID 的普通失败。lookup 没有结果时区分 pending 与 unknown；call 只是同步便利包装。新传输不可透明重发下载。文件校验不是身份认证，运行目录仅本机受信任用户可写；不支持网络共享目录部署。

## 下载任务与校验

默认 job_id 从规范化请求范围生成稳定摘要，用户可显式提供 32 位十六进制 ID。每个 job 用独占文件锁防两个外部进程同时推进；报告原子更新，固定参数与 ID 绑定。每个单元预先确定稳定 download request_id；恢复任务先读回和查原请求状态，不生成第二个下载 ID。

流程为缓存校验→必要时一次下载→外部等待/读回→verified 或 incomplete/failed/unknown。仅安全的只读查询可重复；底层阻塞不可抢占，不在 QMT 内等待数据。默认单元等待 download_timeout=120 秒，有限正数；不可用时保留 report 供续查。一个单元结果未知后停止继续提交新的下载，剩余单元保持 pending，避免积压和未知放大。

校验必须使用原始 time UTC 毫秒，自动模式拒绝只有 stime 的 K 线；原始 time 要求有限整数、唯一。日期必须等于目标日，六个 OHLCV 字段完整且数值有限，OHLC 严格为正且关系合理、量额非负；整日 `volume` 与 `amount` 都全为零时拒绝，但日内有其他活动时允许单个零成交量分钟。日线恰好 1 条；1m 为标准 240 分钟网格，可多 09:30 一条；5m 为对应 48 格，可多 09:30 一条。这些是可观察的结构规则，不是历史停牌或交易日历检测：本 Alpha 没有相应元数据源，合法的整日无活动数据也会记录 incomplete，须由后续业务合同分类。

总任务仅所有单元 verified 且任务级 errors 为空才成功，其报告 state 也使用 `verified`（不新增 `success` 枚举），与 CLI 成功退出条件一致。成功只表示本次所选日期的结构可读性，不是跨源逐值一致、全市场容量或生产验收。completed job 再调用须复核缓存；不能只凭旧报告保证当前可读。新一轮复核必须持久保留未完成标记直到逐项复核结束，不能在 probe 刚成功或只复核部分单元时清除，避免并发状态查询或中断后把旧证据当成新成功。报告不保存全量行情，只保存请求 ID、状态、时间、校验摘要和错误。

## 验收与部署

离线测试覆盖空缓存自动补齐、已有缓存不下载、多个代码/日期分块、旧 worker 拒绝、同 ID 去重、进程重启/ACK 丢失/超时续查、未发布窗口、坏状态、缺字段/错日期/缺分钟/时区、配置边界及旧模式回归。用真实文件传输+合成 ContextInfo 做往返，不把 mock 声明当成功。

新入口在离线验收后才交给用户单独加载；不自动热替换旧入口或重启 QMT。若 UI 不可用，清晰交付加载步骤，实测未完成即保持 pending。不得用仍运行的 qualification_v1 冒充正式 worker 验收。README 和版本说明分别列出代码完成、离线通过和真实终端通过的范围。
