# 本地交付验证记录

当前版本：`0.2.0a1`，实验性内置 QMT 文件桥；默认 `cache_only`，可显式 opt-in 到受限的已结束交易日 K 线自动下载。此记录是离线验证和历史实测记录，不是券商兼容性、业务完整性或生产迁移验收。

## 0.2.0a1 自动下载 CLI 与打包验证

- CLI 合同测试先记录预期 RED：`python -m pytest tests/test_auto_cli.py -q` 为 **5 failed / 1 passed**，失败来自 `download` / `download-status` 尚未注册及本地状态管理器入口不存在；实现后同命令为 **6 passed**。
- 完整离线回归：`python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q` 为 **236 passed in 50.57s**。测试使用临时目录、合成 ContextInfo 和受控文件协议，没有连接真实终端或下载真实行情。
- wheel 构建：`python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist` 成功，最终产物 `bigqmt_data_bridge-0.2.0a1-py3-none-any.whl`，52,885 bytes，SHA256 `b8be4e767930dcd2c2cc0ff9bff9be0d8e96f28368ceeca8c08a657e7ce8d713`。未安装到现有 py10，未发布。
- 在源码目录外的独立临时目录，以 `python -I` 将 wheel 文件直接加入 `sys.path`：成功导入 `bigqmt_bridge`、`bigqmt_bridge.auto_backend`、`qmt_bridge.strategy_auto`，并读取 8 个财务 schema；根 CLI、`download --help`、`download-status --help` 均退出 0。
- wheel 共 25 个条目，包含新 auto 客户端/worker/入口及既有 schema；归档名检查未发现 `experiments`、`evidence`、`config.local`、credentials、运行时 `records/states/client_jobs/response_repairs/requests/responses`。配置样例和文档属于源码发行，不进入 wheel。
- 新 CLI 仅在聚合 `state=verified` 且 job-level `errors` 为空时退出 0；`QmtDownloadError.report` 原样落盘以保留 job/request ID。worker probe/协议失败发生在报告创建后时，manager 保存错误和自动生成的 job/cell ID，不伪造 item 调用状态；下一轮从 probe 前至全部 item 复核结束持续保存 `download refresh in progress`，中断或并发 status 均 fail closed。旧 report 缺 `errors` 兼容为空，损坏的 errors 类型/内容 fail closed。`download-status` 以无 transport 的本地 `DownloadManager` 读取报告，不发送 QMT 请求。报告不包含原始行情行。
- `verified` 只说明请求日期的原始 UTC、字段/数值、OHLC 关系和交易分钟网格满足当前结构合同；没有停牌、上市日期或交易所休市状态数据源，不能据此宣称业务完整或生产可用。
- 正式新 worker 的 live acceptance **未完成**。最近一次 GUI 只读预检在最小化窗口后重新选择与激活时返回 `window is not a usable app window`；没有粘贴、运行或加载 `strategy_auto.py`，没有产生新入口的 probe 或真实下载。此前 P0/P1 结果仅属于旧 `qualification_v1` 实验，不上调为正式验收。

## 0.1.0a1 初始离线验证（历史记录）

- 外部环境：Python 3.10，pandas 2.3.3，pytest 9.0.2。
- 完整离线测试：`python -m pytest tests -q`，**60 passed**。
- wheel 构建：`python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist` 成功。
- 将 wheel 安装到独立临时目标目录，从源码目录以外用隔离模式导入客户端、读取八表 JSON、运行 CLI help，均成功。没有把项目安装到原采集环境的 site-packages。
- 禁用 site-packages 后，内置端 worker/protocol 仍可导入；测试同时检查内置端 Python 3.6 语法。没有宣称用真实 Python 3.6 运行时完成全部测试。
- 技能格式验证器通过；独立智能体演练正确给出另一盘符路径下的配置、probe、sample、native 基线和 compare 命令，并区分探测、数据、容量与上线证据。CLI help 在演练中实际执行，真实 probe/sample 未执行。
- 独立代码审查发现“核心行情数值为空仍可通过”问题：新增测试在修复前为 7 failed / 1 passed，修复后为 8 passed。客户端、原生采样和已保存报告比较均覆盖；财务合法空值不受该行情规则影响。
- 核心源码不再依赖原项目配置、数据库 SQL、第三方 RPC 库。财务 JSON 仅含字段合同，不含数据。
- 源码和 wheel 文件清单检查未发现凭据、真实样本、券商源码或私人路径。扫描命中的旧后端环境变量名和 RPC 名称仅为隔离/拒绝测试的合成输入；发布清单里的 token/webhook 是提醒文字，不是值。
- 原采集仓库已跟踪文件未产生修改；未停止或重载其 QMT 策略。

## 智能体技能验证过程

无技能基线中，独立代理无法从项目名得知准确 CLI，正确指出缺少入口和配置协议；有技能后能够给出实际命令及最小 JSON。演练还识别出 `compare` 没有来源认证，因此文档补充两端生成命令、环境版本和原始报告的来源记录要求，随后独立复核通过。

该技能属于工具参考技能，不是交易自动化或强制纪律技能；不承诺防御任意恶意提示，也不自动赋予安装、客户端重启、数据库写入或发布权限。

## 0.1.0a1 初始阶段未执行 / 仍需实际用户验收（历史记录）

- 该初始离线阶段没有连接真实 QMT、下载历史数据、读取账号、下单或写数据库；后续真实只读与 qualification 实验见下方补充，正式 auto worker 仍未加载。
- 没有验证每个券商版本的原始行情返回结构、财务依赖、完整合约、板块身份或全市场吞吐。
- 没有把技能安装到个人技能目录，没有发布 GitHub / PyPI。
- `compare` 不是来源认证，也不是全面的数据质量或投资判断工具；字段值有限且一致仍不足以证明数据真实、完整或新鲜。
- 单次缓存确认不是持续缓存供应机制；不可直接据此接管生产流水线。

## 打包边界

源码归档仅来自新目录的受审查文件，包含 README、代码、测试、字段 JSON、技能与文档；不包含 `.git` 历史、`dist/build`、Python缓存、临时安装目录、本地配置、日志和运行队列。

此独立版本新增的行情数值门禁没有回写到原采集仓库；若需要同步，应另做原项目回归测试后部署，不能在运行中直接替换。

## 补充：模拟终端启动兼容修复

- 用户日志显示旧策略第 4 行 `ModuleNotFoundError: No module named 'importlib'`；只读检查该模拟终端的 `python36.zip`，没有发现 `importlib` 包。此问题发生在加载策略阶段，尚未调用行情或交易接口。
- 将仅用于重载 worker 的 `importlib` 改为可选。缺少时允许正常导入 worker，并提示升级服务端文件后需在安全窗口重启客户端；其他导入错误不被吞掉。有 `importlib` 时保留原来的 worker 重载行为。
- 新测试使用外部 Python 3.10 的隔离子进程，关闭 site-packages，拦截 `importlib` 导入以模拟缺包。修复前 **1 failed / 1 passed**，失败位置与用户反馈一致；修复后全套 **62 passed**。测试执行真实策略的启动、文件请求/响应、定时回调、重复初始化及停止；没有连接 QMT。
- 更新了 README 的重新加载步骤与重载限制。未修改券商安装文件、原采集仓库或生产配置，未安装依赖、重启客户端或提交委托。
- 这些是离线回归证据，仍需用户重新加载策略后执行真实 probe；不能据此宣称模拟终端已连通或数据/交易迁移验收通过。

## 补充：模拟终端只读连通（北京时间 2026-09-09）

- 用户取消“启动本地python”后启动成功。外部真实 probe 退出码 0，返回内置 Python 3.6.8、worker v2 和 `get_market_data_ex_ori`；返回 PID 与已确认的模拟客户端进程一致。
- 板块树、沪深 A 股成员、单股简版合约和单股指数权重均有真实响应。只验证了所取接口能返回数据，未证明完整合约、分类身份、指数成分全集或跨端逐值一致。
- 用户确认历史缓存尚未下载或不确定，因此未设置 `cache_prepared=true`，本次没有执行日线、分钟线、Tick、复权或财务验收。原生基线此前连接失败，不能宣称跨端对照通过。
- 真实报告只保留在被忽略的本机 `evidence` 目录，不纳入公开源码。没有下单、撤单、写数据库、切换生产任务或推送仓库。

## 补充：P0/P1 自动下载闭环（北京时间 2026-09-09）

- 用户启动独立 qualification_v1 策略后，外部通过全局内置下载函数取得股票、ETF、指数的日线/分钟线及单股 Tick；不需要手工下载，不依赖原生 xtquant 服务。
- 日线各 1 条，分钟线各 241 条；指数日线原有缓存，其余原始历史样本为空后变为可读。单股 Tick 4,846 条；另读取一份实时五档快照。详见[实测结果及限制](research/2026-09-09-p0-p1-results.md)。
- 真实响应暴露两个解析问题：请求字段投影丢弃原始 UTC 时间，以及列数组均空未被识别为冷缓存。新增回归先为 4 failed / 27 passed，修复后全套 `tests/` 加实验测试 **85 passed**。
- 当前终端缺少 `_socket`、`_ctypes`、importlib 等依赖，不能假设两个候选整包可以原样部署；本轮只验证自己的最小文件实验端，没有宣称候选后端通过。
- 保留跨周期量额/价格差异，不作精确一致承诺。当时的 `0.1.0a1` 发行版下载方法尚未改成自动任务接口；该限制已由 `0.2.0a1` 的受限 auto 模式更新，但交易、生产切换和正式新 worker 实测仍未执行，也未推送。
