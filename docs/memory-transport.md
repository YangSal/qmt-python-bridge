# 内存行情通道与磁盘测试消费者

源码和命令均从本项目工作目录运行。日常测试可使用下面的统一策略，同时提供历史文件桥
与内存行情通道；独立入口保留用于资格测试。不要在运行中替换已导入的 Python 模块。

## 目录职责

- 项目目录：源码、测试、文档、忽略的本机配置和非行情证据。
- 历史 runtime：历史请求、缓存及持久下载任务，例如 `D:/bigqmt-market-runtime`。
- 内存 runtime：每次生成的独立 Python 包、启动脚本和私有认证配置，例如
  `D:/bigqmt-memory-runtime/session-001`。这里**不存放行情或通道消息**。
- 消费者输出：用户明确请求的磁盘归档，例如 `D:/qmt-collector-test/realtime`。

私有会话目录必须新建，父目录必须可信。生成器原子设置仅当前用户和 SYSTEM 可读写的
继承 ACL，并校验每级父目录的所有者和替换权限，不修改现有项目目录的 ACL。
共享临时目录或允许其他用户改名/替换的工作目录会被拒绝。可由本机调用
`bigqmt_bridge.pipe_security.protect_private_directory` 创建独立私有 runtime 根目录；
不要使用资源管理器先建一个开放的目标会话目录再写凭据。
QMT 编辑器只粘贴生成的启动脚本；密钥从受保护的配置读取，不粘贴进编辑器。

## M1b 验证流程

外部 Windows Python 3.10+，内置端目标 Python 3.6.8，不安装或复制任何 QMT 二进制依赖。
以下 `python` 指已存在的外部解释器；路径为示例。

```powershell
python -m bigqmt_bridge.memory_session prepare --directory D:/bigqmt-memory-runtime/probe-001
```

用户在 QMT 新建独立探针策略，粘贴生成的 `strategy_probe.py`，取消“启动本地python”，
运行而非回测。看到 `QMT memory synthetic probe ready` 后执行：

```powershell
python -m bigqmt_bridge.memory_session qualify --config D:/bigqmt-memory-runtime/probe-001/session.local.json --report evidence/memory-baseline.json
```

合成阶段没有行情 API 调用。至少 10,000 个样本逐条核对，验证双向 HMAC 挑战、256 KiB
上限、有界队列、损坏/截断/协议/旧会话/重复序号/超长拒绝。空闲和负载分别记录回调
间隔与往返分位数。显式当前用户/SYSTEM DACL、拒绝远程客户端标志、独立进程受限令牌
访问拒绝分别记录；受限令牌测试不冒充另一个真实登录账户的验收。

每次请求默认 15 秒、上限 30 秒，整轮诊断上限 300 秒。任何失败保留已确认门禁，
结果为 `incomplete`。探针代码有自身最长运行期，不应长期运行。

还必须观察用户手动停止时的未完成请求，然后重启：

```powershell
python -m bigqmt_bridge.memory_session watch-stop --config D:/bigqmt-memory-runtime/probe-001/session.local.json --baseline evidence/memory-baseline.json --report evidence/memory-stop.json
```

在有界观察窗口内，用户只停止该探针，记录 `resources_closed=True/False`，再重新运行。
日志确认释放后执行：

```powershell
python -m bigqmt_bridge.memory_session restart --config D:/bigqmt-memory-runtime/probe-001/session.local.json --baseline evidence/memory-stop.json --report evidence/memory-accepted.json --resources-closed-confirmed
```

该参数须来自用户实际确认，不能为了通关自动填写。重启必须产生新实例，拒绝旧会话并
恢复新连接；全部必需项通过才产生 `named_pipe_verified`。不操作其他策略或重启客户端。

## 统一采集入口

完成当前终端的 M1b 验证后，可生成一个同时处理历史和实时的 QMT 策略：

```powershell
python -m bigqmt_bridge.memory_session prepare --directory D:/bigqmt-memory-runtime/collector-001 --qualification evidence/memory-accepted.json --history-dir D:/bigqmt-market-runtime --enable-downloads
```

历史目录与私有会话目录必须分开。下载默认关闭，本例显式启用。生成器复制已验证的
内存核心和历史依赖到新的私有包，不覆盖已加载模块，不更改历史任务 ID。
用户停止自己此前加载的桥接/探针策略，再新建 `BRIDGE_COLLECT_V1`，粘贴生成的
`strategy_collect.py`，取消“启动本地python”，编译并运行（非回测）。
看到 `QMT unified collector v1 ready` 后，只需保留这个采集策略运行；不重启客户端。

单一定时回调先处理内存请求，再处理最多 8 个历史任务，历史任务之间继续处理内存。
每次回调的历史部分设有 20ms 软预算，但不能打断进入 QMT 的同步下载调用；下载期间
实时响应仍可能延迟。因此服务不提供交易，也不承诺并行采集时的硬实时延迟。
连续 3 次历史调度异常会暂停历史部分，`service_status` 可查询该状态。策略最长运行
24 小时，停止时关闭历史锁、订阅和内存资源；这不是生产守护进程。

历史消费者沿用同一历史 runtime 的配置；实时消费者使用新会话的 `session.local.json`。
停止旧策略、加载新策略由用户操作。不要同时让两个策略占用同一个历史 runtime。

## 行情与用户归档

通过实际 QMT 门禁后，使用相同已验证内存核心生成新会话。核心源文件摘要变化需要重测。

```powershell
python -m bigqmt_bridge.memory_session prepare --directory D:/bigqmt-memory-runtime/market-001 --qualification evidence/memory-accepted.json
```

用户新建独立行情策略并加载生成的 `strategy_market.py`，看到
`QMT market memory v1 ready` 后运行：

```powershell
python -m bigqmt_bridge.realtime_collector --config D:/bigqmt-memory-runtime/market-001/session.local.json --codes 000001.SZ,510300.SH --output-dir D:/qmt-collector-test/realtime --seconds 60 --interval 0.5
```

该测试行情策略最多运行两小时，外部单次采集最多一小时。期满由用户安排新的测试运行，
这不是无人值守生产守护进程。

消费者通过命名管道接收最新快照，之后才将收到的数据保存为 gzip JSONL。行情不进入
IPC 文件、调试日志或诊断报告。每批归档附 SHA256、计数、源时间变化和退订结果；
超时保留部分归档并标记断开，不将上次行情冒充最新。`state=complete` 表示采集过程完成，
还要看 `realtime_verified`：每只证券均需新鲜行情及实际源时间更新，静态缓存不算通过。

当前模式明确为 `poll_snapshot`：最多 10 只证券，原生订阅、最新快照轮询、退订、状态与
健康查询；不保证轮询之间的每笔更新完整，不开放全推。完整字段、年龄与盘口语义见
[行情合同](market-memory.md)。没有账户或下单功能。价格、量额单位仍需终端字段对照，
一次短采样不等于一个交易日稳定性或生产迁移验收。

协议使用 Windows 消息型 overlapped 命名管道；每端最多一个未完成读和写、8 个发送帧。
取消后保留整个 OVERLAPPED 所属对象直到内核完成，不能把取消请求当作已释放。
QMT API 仅由策略定时器线程调用，原生同步 API 无法被外部超时取消。
