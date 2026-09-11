# 正式自动 K 线入口：首次真实终端小样本验证

北京时间 2026-09-11 14:52–14:55。源码基线 `233f368`，入口为
`qmt_bridge/strategy_auto.py`，不是早期 `qualification_v1` 实验。

## 环境与范围

- 用户在其已登录的模拟 QMT 中手动新建、粘贴并运行策略；界面操作由用户负责。
- 外部 Python 3.10 使用 `config.auto.example.json`，独立历史运行目录
  `D:\bigqmt-auto-runtime`。未使用 native xtquant 后端。
- 外部 probe 返回 Python 3.6.8、`auto-kline-v1`、worker version 3、
  `downloads_enabled=true` 和原始读取入口 `get_market_data_ex_ori`。
- 探针进程名为 XtItClient；系统进程查询未返回可执行文件路径。因此本轮模拟端身份
  依据用户的手动部署确认，不声称完成了独立的进程路径身份认证。没有查询账户或交易。
- 请求日期固定为已结束交易日 `20260908`，没有删除缓存制造冷启动。

## 结果

| 证券 | 日线 | 1 分钟 | 5 分钟 |
|---|---|---|---|
| 000001.SZ | 1 条，缓存命中 | 241 条，缓存命中 | 48 条，触发下载后通过 |
| 510300.SH | 1 条，缓存命中 | 241 条，缓存命中 | 48 条，触发下载后通过 |
| 000300.SH | 1 条，缓存命中 | 241 条，缓存命中 | 48 条，触发下载后通过 |

九个证券×周期单元的下载任务报告均为 `verified`，job-level `errors` 为空。
三笔实际底层下载的持久状态均为 `returned`，`late=false`，随后才通过数据读回校验。
不能把另外六个缓存命中单元写成真实下载成功，也不将下载函数返回值当成数据就绪证据。

另用正式外部 facade 的 `sample` 命令读取三证券×三周期，日期、时间、字段和数值检查
通过，退出码 0。该自校验不是与独立数据源的对照，不证明跨源逐值一致或业务完整性。

同一股票 5 分钟 job ID 再次执行后仍通过，使用相同 request ID，历史目录仍只有原来的
三笔 `download_kline` 请求记录；未新增下载请求。本地 `download-status` 也返回成功。
这仅验证正常完成任务的重复调用，不代表崩溃、断线、客户端重启等异常恢复已经实测。
检查时 `requests/`、`running/` 无未完成请求。

## 可复现命令

在源码目录中使用外部 Python（本机实际使用 conda `py10` 的解释器），例如：

```powershell
python -m bigqmt_bridge probe --config config.auto.example.json --output evidence/probe.json
python -m bigqmt_bridge download --config config.auto.example.json --codes 000001.SZ --period 5m --start 20260908 --end 20260908 --output evidence/download.json
python -m bigqmt_bridge sample --config config.auto.example.json --date 20260908 --codes 000001.SZ,510300.SH,000300.SH --periods 1d,1m,5m --families market --output evidence/sample.json
```

复跑可能命中已有缓存。续查使用原报告的 job ID，不换 ID 绕过未知状态。
本次带唯一文件名的原始报告保留在 Git 忽略的 `evidence/20260911-auto-*.json`；
原始行情、进程标识及运行队列不提交公共仓库。

## 验收边界及下一步

本轮完整离线回归 `python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q`
为 **254 passed in 52.57s**。此前另一次状态审查运行曾出现自动下载用例超时（253 passed /
1 failed），该用例单独复跑通过；本轮没有修改运行逻辑或测试预算。因此当前全绿并不证明
此前的时间敏感问题已消除，仍需跟踪。

正式入口已从“未加载”推进到“单日小样本连通、自动下载与读取通过”。仍未完成：
全市场/多日容量、长时间运行、故障恢复、其他券商兼容、跨源对照、生产采集切换。
本轮日线和 1 分钟未触发下载，其正式入口的冷缓存下载仍需另测。
交易查询、下单、撤单、成交回报、实时订阅和内存通道均未实现/验收。

用户已明确外部程序和 QMT 同机部署。后续交易实时行情必须使用经验证的内存通道，
不写独立 IPC 行情文件，也不能静默回退文件模式；现有历史文件通道不因此冒充实时通道。
共享内存可用性、并发一致性和更新延迟尚未在该内置解释器中验证。

本轮没有自动操作 QMT 界面、下单、撤单、写生产数据库、修改原采集调度、重启客户端、
安装依赖、构建安装包或清理缓存。只补充上述三个 5 分钟历史缓存并保存本地验收证据。
