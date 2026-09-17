# 统一采集入口：真实终端测试

测试日期：2026-09-17（北京时间）。内置 Python 3.6.8，外部 Python 3.10.20。
本文只记录脱敏统计，不附原始行情、账户、密钥或本机配置。

## 入口与范围

用户停止此前本项目的三个独立策略后，运行一个 `BRIDGE_COLLECT_V1`。
生成器把未改动的合格内存核心、历史 worker 和新的统一适配器复制到独立私有包；
脚本和私有配置均位于 D 盘运行目录，源码位于项目目录。不热替换已加载模块。
一个 QMT 定时回调处理历史请求和内存行情，不调用账户或交易接口。

## 实测结果

| 检查 | 结果 | 范围 |
|---|---|---|
| M1b 合成与恢复 | named_pipe_verified | 10,000 合成样本；真实停止、用户确认资源释放、新实例、旧会话拒绝、新连接恢复 |
| 历史 probe | 通过 | 同一个统一入口、沿用原历史 runtime |
| 实时股票与 ETF | realtime_verified=true | 000001.SZ、510300.SH；60 秒，与历史采集重叠运行 |
| 实时归档 | 通过 | 118 组快照、236 条行情；236 条均新鲜；无不完整快照；gzip 可读且 SHA256 一致 |
| 实时变化 | 通过 | 两证券分别 20 / 19 次源时间向前变化，共 39 次；不是静态缓存验收 |
| 显式退订 | complete | 采样结束后两证券均正常退订 |
| 连接直接断开 | 通过 | 原生订阅及行情状态清空，新连接的 generation 改变，无清理积压 |
| 断连后重新订阅与采样 | 通过 | 再采样 10 秒，20 组快照、40 条新鲜行情、6 次源时间变化，退订 complete |

首次连接检查显示历史已正常处理此前积压的 20 项请求。运行中的服务状态保持
`history_suspended=false`、`market_cleanup_pending=false`、`trading_ready=false`。
测试期间观察到最长回调约 155ms；20ms 只是两次历史调用之间检查的软预算，不能中断
已进入 QMT 的同步调用，不能据此承诺生产实时延迟。

## 历史与复权

冻结范围为 2026-08-13、2026-08-14，沪深北 A 股、ETF、指数，共 7,872 证券。
日线、1 分钟、5 分钟各两个日期，加每证券一份截止目标日的复权事件，共 55,104 单元。
此次恢复复用原下载任务 ID，对失败的只读复权请求按已记录的终态进行有审计刷新。

全量任务仍为 incomplete，不能把已恢复续采等同于全市场完成。例如 `000012.SH`
分钟线任务确实调用下载接口，但读回仍未通过 `incomplete/nonstandard K-line grid`
校验；该结果保留为失败，不自动解释为无权限、停牌或券商没有数据。
缓存命中、下载尝试与最终验证分别保存在原任务和单元报告中。

## 可复现程序

按[内存通道说明](../memory-transport.md)完成当前终端的资格测试和统一入口生成，
再从仓库根运行以下源码入口。配置路径和归档目录应替换为自己的环境。

```powershell
python -m bigqmt_bridge probe --config config.market.local.json --timeout 5 --output evidence/history-probe.json
python -m bigqmt_bridge.realtime_collector --config D:/bigqmt-memory-runtime/collector-001/session.local.json --codes 000001.SZ,510300.SH --output-dir D:/qmt-collector-test/realtime --seconds 60 --interval 0.5
python -m bigqmt_bridge.market_collector run --config config.market.local.json --output-dir D:/qmt-collector-test/market --max-units 500 --max-seconds 300 --retry-failed
python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q
```

全市场目录必须先使用 `market_collector plan` 冻结范围，详见[全市场采集说明](../market-collector.md)。
资格测试、采集程序和自动化测试均交付源码；真实测试产物不提交 Git。

提交前完整命令结果为 **513 passed in 91.85s**，`git diff --check` 通过。

当前行情合同为 `poll_snapshot`，每次最多 10 只证券，不保证轮询之间每笔更新完整。
两证券短采样不代表全市场实时能力、全天稳定性、跨源逐字段对照或交易就绪。
