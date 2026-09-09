# P0/P1 临时验证工具（非生产版本）

用途：为复审报告中的候选选型提供本机证据。这个实验不代表已经采用或安装 xtquant_big_convert / CFQuant，也不替换已发布的只读 worker。

先证明目标终端有可用的内置下载入口和允许的依赖，再决定采用哪个候选后端。没有能力的 API 不能靠兼容函数名补出来。

本机 2026-09-09 的[实测结果](../../docs/research/2026-09-09-p0-p1-results.md)：股票/ETF/指数单日日线、分钟线自动下载读回通过，单股 Tick/快照有真实结果；当前依赖探测不满足直接部署 Redis/ZMQ/ctypes 桥接的条件。此结论仅适用于本次环境和样本，不代表交易或生产迁移完成。

## 安全范围

- 与原桥分开：原运行目录保持不变，新目录为 `D:\bigqmt-qualification-runtime`。
- 只提供 `capabilities/modules/read_history/download_history/full_tick`，不提供账户写入、下单、撤单或任意代码执行入口。
- 下载/读取限于 `000001.SZ`、`510300.SH`、`000300.SH`，日期固定为已完成交易日 `20260908`，周期为 `1d/1m/5m/tick`。
- 不导入原生 xtquant，不升级内置 Python，不改变券商权限或终端配置。
- 单个定时回调仅处理一个请求；不在 QMT 内启动工作线程。下载可能阻塞底层调用，故先只测单股、单日；不得盘中发全市场任务。
- 启动时不自动下载。只有外部 `sample --download` 明确请求才调用下载函数，调用超时不自动重发；未完成请求保留在 running 中，不在重启后重放。
- 运行目录应只允许当前用户/QMT 运行用户访问，不放共享目录、不暴露公网。

## 1. 在模拟 QMT 加载一次入口

新建一个与当前 STRATEGY 不同名的 Python 策略，例如 `BRIDGE_QUAL_V1`。粘贴本目录 `strategy.py` 的全部内容，保留 PROJECT_ROOT 和 RUNTIME_ROOT。

取消“启动本地python”，使用内置标准模型运行。看到：

```text
QMT qualification v1 ready; trading disabled: D:\bigqmt-qualification-runtime
```

此时只是定时服务就绪，尚无下载动作。不需要寻找“数据管理”，不需要手工下载历史数据。无需重启整个 QMT，也不要停止其他生产策略。

本实验不做热重载；若加载后再修改 worker，不能假设仅重跑入口就能加载新代码，应安排新的实验模块版本或在安全窗口明确重启。不要在运行中覆盖模块来测试变更。

## 2. 外部 Python 获取能力证据

在 `D:\bigqmt-data-bridge` 执行：

```powershell
& C:\Users\yangming\.conda\envs\py10\python.exe -m experiments.qualification_v1.client capabilities --output evidence/20260909/qualification-capabilities.json
& C:\Users\yangming\.conda\envs\py10\python.exe -m experiments.qualification_v1.client modules --output evidence/20260909/qualification-modules.json
```

capabilities 区分注入的全局函数与 ContextInfo 方法；trade callable 存在不代表账户交易能力验收。

modules 只做固定清单的 import 探测，不安装依赖、不启动 Redis、不打开网络连接。import 成功不代表通信权限或线程调用安全已验证。

## 3. 单股自动下载闭环

确认能力和模拟终端状态后，依次执行，不并发：

```powershell
& C:\Users\yangming\.conda\envs\py10\python.exe -m experiments.qualification_v1.client sample --code 000001.SZ --period 1d --download --output evidence/20260909/qualification-000001-1d.json
& C:\Users\yangming\.conda\envs\py10\python.exe -m experiments.qualification_v1.client sample --code 000001.SZ --period 1m --download --output evidence/20260909/qualification-000001-1m.json
```

流程：先读缓存并保存原始结果 → 调用一次内置下载 → 在外部等待并反复读取 → 校验日期、字段、OHLCV、时间唯一性及分钟网格。

- 下载函数返回 None/True 不等于成功取到数据。
- `ok=true`：该样本读回校验通过，不是整个迁移或第三方候选通过。
- `cold_cache_to_ready=true`：本次下载前返回为空，下载后读回通过；如果原来已有数据，不宣称冷缓存验证。
- 分钟网格按正常完整交易日的收盘标记检查（可包含额外 09:30 bar）；如果终端采用另一种标记方式，显式报差异，先核实再调整，不静默放行。
- Tick 仅检查基础结构和时间，不证明全天分笔无缺漏；五档全集及吞吐仍属于后续验收。
- 服务器错误、下载超时或旧日期不可取均不得当成空市场数据处理，也不产生生产 `.empty` 标记。

去掉 `--download` 可只读复核缓存；这是独立诊断选项，不是长期自动采集前提。

## 4. 离线测试

```powershell
& C:\Users\yangming\.conda\envs\py10\python.exe -m pytest experiments/qualification_v1/test_qualification.py -q
```

测试通过只说明本实验工具的参数、安全范围和文件往返逻辑符合预期，不证明券商接口可用。该目录不进入当前发布包，不提交真实响应数据到 GitHub。
