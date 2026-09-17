# 外部磁盘采集客户端实测

测试时间：2026-09-17 UTC 01:07 起（北京时间 09:07 起）。
外部 Python 3.10.20，用户手工启动现有 BRIDGE_AUTO_V1 并确认 ready；
随后外部 probe 成功。本轮没有修改或热重载嵌入端代码。

## 实际范围与结果

证券：000001.SZ、510300.SH、000300.SH。周期：1d、1m、5m；两个独立单日任务。

| 北京日期 | 归档文件 | 行数 | 本次下载接口执行并返回 true、随后严格读回 | 本次缓存命中 |
|---|---:|---:|---:|---:|
| 2026-09-08 | 9 | 870 | 0 | 9 |
| 2026-09-15 | 9 | 870 | 7 | 2 |

每个日期各包含 3 行日线、723 行 1 分钟线（含集合竞价首条）、144 行 5 分钟线。
18 个 gzip JSONL 文件共 1,740 行，独立解压后核对行数、SHA256、证券/日期与整数 UTC
毫秒时间。价格和量额使用源口径、不复权，未做独立行情源的逐值对照。

09-08 的 000001.SZ/5m 复用了 09-11 已存在的持久任务，该任务保留 attempted=true
及原生 returned 证据；**这是旧下载证据，不能计为本次下载**。
09-15 新建任务的 7 项下载在 worker 状态中均为 returned、API 为 download_history_data、
返回 true，并随后完成缓存严格读回与归档；000300.SH 的日线和 1 分钟线命中缓存。
原生返回本身不保证数据已就绪，以上成功同时依赖后续读回证据。

## 故障与续采

09-15 首次执行已保存 8 个文件，最后一项 510300.SH/5m 在下载前读取缓存请求的状态
文件时遇到 Permission denied，任务进入 unknown，采集报告为 incomplete。
原始失败清单已留存于本机忽略的 evidence，未把失败改记为成功。

保持原命令、范围、输出目录和下载任务 ID 续采，先验证已有 8 个文件，再对账最后单元；
该单元首次真正调用下载后读回成功，最终 9/9 保存。没有更换 ID 重发未知原生下载。
这个实测只证明本次访问错误后的恢复，不证明 Windows 文件访问竞态已经修复。

两个已完成任务再次运行均只做本地文件验证，桥 records 集合没有新增请求；
`action=local_verified` 不能解释为本次重新探测 QMT 或重新下载。

## 可复现命令

使用现有外部解释器，在仓库根运行：

```powershell
python -m bigqmt_bridge.collector history --config config.auto.example.json --codes 000001.SZ,510300.SH,000300.SH --dates 20260908 --periods 1d,1m,5m --output-dir D:/qmt-collector-test/sample-20260908 --max-seconds 120
python -m bigqmt_bridge.collector history --config config.auto.example.json --codes 000001.SZ,510300.SH,000300.SH --dates 20260915 --periods 1d,1m,5m --output-dir D:/qmt-collector-test/sample-20260915 --max-seconds 180
python -m bigqmt_bridge.collector realtime-check --output-dir D:/qmt-collector-test/realtime
```

归档、私有清单及原始证据均不提交 Git；公开记录只给范围、统计和失败类别。
机器/路径不同时先按实际部署调整配置。

## 离线与未执行门禁

新增 23 项测试通过；独立代码审查发现的全文件预检顺序问题已通过 RED/GREEN 修复并复审。
完整命令 `python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q`：
**310 passed（46.44 秒）**。`git diff --check` 通过。

初始未修改代码的回归曾出现 286 passed / 1 failed，失败用例单独重跑通过；
后续完整回归为 304 passed、最终新增更多测试后 310 passed。未把初次失败隐去或声称
已修复其尚未确认的时序根因。

实时能力检查返回退出码 1：state=incomplete、transport_verified=false、received_messages=0。
M1b 通道、实时首帧/持续更新/恢复仍未实现或验收。全市场、多日连续容量、Tick、财务、
原 datacollect 全量迁移、跨源数据一致性与生产采集均未验收。
