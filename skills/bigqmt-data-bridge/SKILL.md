---
name: bigqmt-data-bridge
description: Use when an agent needs to install, probe, sample, compare, or troubleshoot the bigqmt-data-bridge project's read-only bridge between external Python and QMT embedded Python, including cache errors, timeouts, and migration validation. Not for trading or general xtquant automation.
---

# BigQMT 数据桥参考

本技能服务于开源项目 `bigqmt-data-bridge`，外部包名 `bigqmt_bridge`，内置包名 `qmt_bridge`。它不是完整 xtquant 兼容层。先完整阅读本文件；需要更详细部署或接口说明时，读取用户指定源码根目录的 README.md 和实际 `--help`，不假设技能目录就是源码目录。

**适用范围仅为 legacy/cache_only。** 本文“download_* 只确认缓存”等结论不适用于
独立 auto 后端。新增外部磁盘采集测试程序见源码根的 `docs/collector.md`，其终端证据见
`docs/research/2026-09-17-collector-live.md`；这些链接不表示本技能已验证 auto 或实时
工作流。不要用本技能的旧策略/缓存前提配置新采集器，也不要自动安装技能。

全市场消费者见 `docs/market-collector.md`；命名管道、独立版本化运行目录、实际 QMT
合成/重启门禁和实时消费者见 `docs/memory-transport.md`，行情语义见
`docs/market-memory.md`。这些新入口有独立配置和验收报告，不继承本文 cache_only 的
文件 IPC 或人工缓存语义；本 Skill 的旧操作流程仍不宣称覆盖新入口。

## 定位与环境

从任务取得源码根目录、外部 Python 3.10+ 解释器、配置或 IPC 目录。若无法确定，先只读检查用户指定目录，仍缺失则询问；不要搜索或加载其他私人项目配置。

外部安装命令在源码根目录为 `python -m pip install -e .`；开发测试可用 `".[test]"`。安装前遵循当前任务的授权。内置端不运行 pip：QMT 策略编辑器加载 `qmt_bridge/strategy.py`，`PROJECT_ROOT` 指向源码根，`BRIDGE_DIR` 指向独立本机 IPC 目录。需用户操作客户端时给出具体步骤，不声称已点击或重启。

## 配置合同

CLI 只读取显式 `--config` 的扁平 JSON，没有 `qmt` 包装键，不自动加载配置或原项目环境变量。配置示例在源码根 `config.example.json`，默认 `cache_prepared=false`。只有确认对应日期、代码、周期的 QMT 本地缓存已准备，才将本地配置改为 true；此值是人工确认，不是下载或新鲜度检测。

配置中的相对 `bridge_dir` 相对于配置文件目录；命令行相对目录则相对于当前工作目录。优先使用明确绝对路径。不要将新测试桥指向正在使用的其他 IPC 目录。

新路径的最小本地配置示例（`config.local.json`）：

```json
{"backend": "file_bridge", "bridge_dir": "E:\\qmt-ipc", "cache_prepared": false}
```

## 常用命令

以下为 PowerShell 示例，替换为任务实际路径；`20260904` 是固定北京时间日期示例，不是自动选择的“今天”。使用外部解释器运行。

```powershell
Set-Location E:\research\bigqmt-data-bridge
$bridgePython = '.\.venv\Scripts\python.exe'
& $bridgePython -m bigqmt_bridge probe --config config.local.json --timeout 5 --output evidence\probe.json
# 核对缓存后，配置里的 cache_prepared 应为 JSON true
& $bridgePython -m bigqmt_bridge sample --config config.local.json --date 20260904 --codes 000001.SZ --periods 1d --families market --output evidence\bridge-daily.json
& $bridgePython -m bigqmt_bridge compare evidence\native-daily.json evidence\bridge-daily.json --output evidence\diff.json
```

如果只拿到了 IPC 目录，probe 可用 `--bridge-dir E:\qmt-ipc` 而不指定配置。取样需要显式配置中的缓存确认；不要发明 `--cache-prepared` 参数。

原生基线在仍可用的授权原生环境执行：`python -m bigqmt_bridge sample --backend native --date 20260904 --codes 000001.SZ --periods 1d --families market --output evidence\native-daily.json`。可从另一节点带回样本，不为此切换生产登录模式。没有有效原生样本时记录缺少基线，不能宣称对照通过。

| 任务 | 入口与事实 |
|---|---|
| 行情 | `sample --families market --periods 1d/1m/5m/tick`，实际填写单一周期或逗号列表；显式日期、最多10个代码 |
| 其他样本 | `--families divid/financial/instrument/sectors/weights`，填写单一类别或逗号列表 |
| Python 调用 | `create_backend(load_config(path))`，分别来自 `bigqmt_bridge.backend/config` |
| 验证 | `compare` 返回0才是样本对照通过，1表示失败；空样本不能通过 |
| 离线测试 | 源码根运行 `python -m pytest tests -q`，需已安装测试依赖 |

行情读取固定 `subscribe=False, fill_data=False`，时间为 UTC 毫秒，转换使用 `utc=True` 再 `tz_convert('Asia/Shanghai')`。比较需要同代码、日期、周期、字段、单位、复权口径，保留重复行和数组次序，不通过删列/去重/猜单位让差异消失。

`compare` 不校验文件来源身份。跨端验收时保留两端真实生成命令、项目/解释器/客户端版本及原始报告；复制同一报告后的相等不能证明独立原生对照。

## 探测与故障判定

- 桥 probe 的 `ok:true` 仅证明定时往返和方法存在；检查 `worker_version:2`、`market_reader`、各项 capabilities。原生 probe 的 `import_only:true` 连真实连通性都未证明。
- 原始行情路径优先 `get_market_data_ex_ori`，缺失时退到内置 `get_market_data_ex`；券商封装可能要求 pandas。财务八表、完整合约、分类 ID 和指数成员必须单独验收。
- `download_*` 只是缓存确认入口，不会下载。缓存为空或 `QmtDataError` 应报告失败，不能写成空交易日、零成员或删旧快照。
- timeout：保留请求编号和错误，检查路径、权限、策略定时器及客户端状态。外部超时不能终止内置 API；停止堆积请求，不删活跃锁或抢占 worker。需要停止策略、修改客户端依赖、重启、清理时，先确认影响与授权。
- 每2秒一个请求，Tick 每请求1股；5000股调度下限约2.8小时，不能承诺全市场吞吐已达标。响应未压缩 JSON 上限64MiB，超大窗口需缩小，未实现自动拆分。

## 输出与任务边界

交付实际命令、退出码、证据路径、验证范围及未通过项；区分模拟测试通过、probe通过、真实取样通过、跨端对照通过。维护结束不等于结构性兼容问题消失。

默认只读诊断和小样本，不自行触发下载、下单、数据库写入、双写切换或 GitHub 发布。正式切换需要逐通道数据和容量验收；原生权限仍有效才具备原生回退条件。IPC 目录必须可信且有受控权限；SHA256不是身份认证。公开 issue 不附账号、真实数据、运行目录或日志中的敏感信息。
