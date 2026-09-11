# 大 QMT 内置 Python 通信能力清单实测

测试日期：2026-09-11；报告时间为 UTC 07:35（北京时间 15:35）。
范围：M1a 模块/函数存在性检查，不是内存传输、行情或交易验收。

## 执行及证据边界

用户在已登录的模拟 QMT 中手工加载 `MEMORY_INV`，取消“启动本地python”，
执行 `experiments/memory_v1/strategy_inventory.py`，随后确认已运行。
本地收到了与本次指定会话一致的 `memory-inventory-v1` 报告，外部校验器读取成功，
退出码为 0；输出为 `state=incomplete`、`transport_verified=false`。
模拟账户性质依据用户确认；本次没有查询账户来独立核实。

原始报告保存在被 Git 忽略的 `evidence/memory-v1` 中，不进入开源仓库。
报告不包含账户、行情或订单；本文仅保留脱敏能力摘要，不公布会话、进程标识。
诊断入口是一次性脚本，保存后结束属于正常行为，无需反复运行同一会话。

## 实际发现

内置解释器报告版本为 Python 3.6.8。

| 模块 | 本机内置结果 | 目前能说明什么 |
|---|---|---|
| `mmap` | 可导入，`mmap` 可调用 | 有共享内存候选入口；尚未创建或交换数据 |
| `_winapi` | 可导入，管道创建/连接/读写/关闭/等待等检查项可调用 | 有命名管道候选入口；尚未验证实际调用 |
| `_winapi` 互斥函数 | `CreateMutex`、`ReleaseMutex` 不存在 | 不能按这组互斥函数方案同步共享内存 |
| `_multiprocessing` | 不可导入，`SemLock` 不可用 | 不能默认依赖其跨进程同步设施 |
| `ctypes` | 导入失败，缺 `_ctypes` | 不向内置环境补入二进制模块 |
| `socket` | 导入失败，缺 `_socket` | 不能在内置端直接套用通常的 socket 方案 |
| `msvcrt` | 可导入，`locking` 可调用 | 文件锁存在性不等于满足无文件内存同步要求 |

QMT 入口存在性：

- `ContextInfo`：`run_time`、`schedule_run`、`subscribe_quote`、
  `subscribe_whole_quote`、`get_full_tick` 可调用。
- 策略全局：`get_trade_detail_data`、`passorder`、`cancel` 可调用。
- 这里只检查 `callable`，没有调用以上函数；不能据此宣布订阅、交易或定时运行通过。

## 结果和下一阶段门禁

只有 `inventory=complete`；`cross_process`、`synchronization`、`security`、
`load`、`recovery`、`scheduling` 全部 `not_run`。

下一阶段仍按已批准设计验证候选，不把当前结果直接升级成“共享内存可用”或“管道可用”：

1. 共享内存需先找到并验证可靠的跨进程同步方式；不能使用 GIL、普通整数或校验和代替。
2. 命名管道需验证有界处理、实际往返、身份隔离及资源关闭；不采用默认权限作为安全证据。
3. 通过合成消息、故障、恢复和无行情调度验证后，才能进入正式行情和只读交易阶段。

本次未创建通信内核对象、未订阅行情、未查询资金、未下单或撤单；没有停止或修改
正在运行的历史数据桥接策略，也没有切换生产采集任务。

## 实施前官方资料核对

- Windows 匿名映射可用 `fileno=-1`，但映射创建本身不提供本项目所需的完整同步合同。
  版本兼容性仍应对照内置 3.6.8，而不是套用新版本参数。
  [Python mmap 文档](https://docs.python.org/3/library/mmap.html)。
- 管道默认安全描述符可能给予 Everyone/匿名用户读取权限，因此不能直接充当仅受信任
  本机身份可接入的证明。[Microsoft 管道权限文档](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)。
- 管道默认等待可能阻塞；异步 I/O 与非阻塞等待不是同一概念，应分别验证处理和取消行为。
  [Microsoft 管道模式文档](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-type-read-and-wait-modes)。
- CPython 3.6.8 的 `_winapi` 源码提供进一步核对重叠读写及返回对象的依据，仍须在本机
  内置端实测，源码存在不代表券商构建完全一致。
  [CPython 3.6.8 源码](https://github.com/python/cpython/blob/v3.6.8/Modules/_winapi.c)。
