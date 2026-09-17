# 内存行情应用 v1：指定证券订阅与轮询快照

`qmt_bridge/market_memory_v1.py` 是 Python 3.6 兼容的只读应用层，由内存 worker 的 QMT
定时回调串行调用。它不建立连接、不替代 M1b 验收。实际启用必须先通过同一部署的内存
通道门禁；离线测试不能作为真实 QMT 快照、持续更新或完整 Tick 交付证据。

M1a 清单没有 `_thread`/锁及回调线程同步的验证。本应用仅实现 `mode=poll_snapshot`：
先真实调用 `ContextInfo.subscribe_quote(stock_code=code, period='tick', dividend_type='none',
result_type='dict', callback=None)`，然后由请求触发 `ContextInfo.get_full_tick(stock_code=codes)`。
无 Python 行情回调、后台线程、回调队列或未经验证的锁；调用方必须维持单一 QMT 定时器所有者。
这不是完整逐笔或增量流，两个轮询之间可能丢失更新，丢失数量无法观测。

[官方行情接口文档](https://dict.thinktrader.net/innerApi/data_function.html) 给出了上述参数、
`get_full_tick` 的逐代码字典结构、订阅整数 ID 与按 ID 退订接口。本实现只接收正整数 ID。
没有整市场订阅、账户查询、下单或交易准备就绪接口。

## 构造与连接生命周期

```python
from qmt_bridge.market_memory_v1 import MarketApplication

application = MarketApplication(C, max_age_ms=5000, timetag_timezone='Asia/Shanghai')
# Only the owning QMT timer calls:
reply_kind, reply_body = application('market_snapshot', {'codes': ['000001.SZ', '510300.SH']})
# The transport owner calls on every disconnect/stop:
cleanup = application.close()
```

`timetag_timezone` 默认 None；示例中的上海时区是显式配置，不是自动识别。可选值为
`Asia/Shanghai`、`+08:00`、`UTC`。配置来源和真实终端观测应在部署证据中记录。
原生数值 `time` 存在且为有效 UTC 毫秒时优先使用；只有 `timetag` 时，不配置时区就保留
原字符串并标记 `time_unknown`。不会使用机器本地时区猜日期。

`close()` 先清除全部行情有效性，重置桥序号并生成新的 `generation`，然后尝试退订自己
拥有的最多 10 个 ID。它返回 `state`、`remaining_subscriptions` 和无行情正文的错误代码。
原生退订正常返回 None 或 True 才移除 ID；异常、False、未知返回值均保留为 `cleanup_failed`。
再次 `close()`/退订使用同一 ID 继续清理，不停止其他代码的清理。无法取得 ID 的未知订阅
仍明确保留。对象可供新连接复用，已有状态不得当作新连接首帧。

订阅调用抛错或返回未知 ID 时不能知道原生调用是否已生效，因此相同代码不会自动重新订阅。
未知项计入容量，需要按终端状态核对。原生调用同步执行，最多 10 项不表示其耗时有硬上限；
外部等待超时不能取消已经开始的 QMT 调用。

## 请求与回复

应用实现 `application(kind, body) -> (reply_kind, reply_dict)`；回复类型是请求类型加 `_reply`。

| kind | body | 作用 |
|---|---|---|
| `market_snapshot` | `{"codes":["000001.SZ"]}` | 一次完整最新快照请求，最多 10 个唯一代码 |
| `market_subscribe` | 同上 | 指定证券原生订阅；全对象最多 10 个活动/未知代码 |
| `market_unsubscribe` | 同上 | 只退订本对象记录的对应 ID |
| `market_status` | `{}` | 订阅、最后观察元数据与年龄；不再请求 QMT 行情 |
| `market_health` | `{}` | 与 status 相同的健康视图 |

代码只接受 `六位数字.SH/SZ/BJ`。格式校验不证明其品种分类、权限或是否上市。
未知请求、额外参数、重复/空/超过 10 个代码在原生调用前拒绝。逐代码数据缺失与原生错误
以结构化错误回复，不将缺数据错误扩大为整个内存通道断开。

所有回复含 `schema=qmt-market-memory-v1`、`mode=poll_snapshot`、`generation`、
`incremental_complete=false`、`missed_updates=null`、`source_sequence_available=false` 和
`trading_ready=false`。源数据未承诺可用的事件序号；桥序号仅计数本对象的逐代码观察，
不能用它计算交易所行情缺口。

快照回复附 `received_at_utc_ms`、`quotes`（按代码映射）与 `state`。此 `state=complete`
只表示请求代码的快照字段结构完整，不表示新鲜、盘中持续更新或交易可用。每条 quote 包含：

- `code`、`bridge_sequence`，及原始有界 JSON 数据 `data`。
- `source_time_utc_ms`、`source_time_basis`、`timetag_timezone`、`received_at_utc_ms`。
- `source_age_ms`、`effective_source_age_ms`、`last_receive_age_ms`、`health` 和 `fresh`。
- `complete_frame`、`book_state`、`missing_fields`、`out_of_order`、`duplicate`。

缺代码/原生异常/无法安全转换的原生对象没有 `data`，含有明确 `error`，并且 `fresh=false`、
`complete_frame=false`。原生异常只保留异常类型组成的错误代码，不复制异常消息或行情正文。
订阅回复提供 `subscriptions`（代码对应 ID、state、error）和 `snapshot_required=true`；
订阅成功本身不是完整首帧。status/health 仅给观察元数据，不含 `data` 盘口正文。

## 字段与健康含义

最小完整快照要求 `lastPrice, volume, amount` 与 `askPrice, bidPrice, askVol, bidVol`。
核心值必须为有限数值，量额不能为负；盘口四数组必须各有五个非负数值。合法零档保留为零，
缺档不补零，缺数组标记 missing，错误数组标记 invalid。零最新价另标记 `zero_price`，
不因“结构完整”就当作新鲜可用报价。其他字段原样保留；不换算原生价格或量额单位。

源时间、字段完整性、倒序和年龄共同决定 `fresh`。过老为 stale、未来时间为 clock_skew、
无法解析时区为 time_unknown、源时间倒退为 out_of_order；错误首帧不会进入有效报价状态。
未来时间不会更新排序水位。`source_age_ms` 是当前 UTC 墙上时钟减源时间；
`effective_source_age_ms` 还采用此前 UTC 基准加单调时钟累计时间，取更保守的年龄。
健康判断使用后者，重复轮询也不重置源时间年龄。status 另给单调时钟下的最后观察年龄；
不会在系统墙上时钟停滞时，将“原本已有一定年龄”的行情再续期到新鲜状态。
UTC 大幅调整后此保守基准可能持续显示陈旧，应核对系统与源时间；本应用没有交易许可。

重复报价不自动诊断断线；静市、休市、停牌和失联不能只从不变价格判断。连接与心跳健康
由内存 transport 管理；外部端断连时必须丢弃缓存有效性，新连接获取新 generation 的完整首帧。

每次最多转换 10 个报价，每报价限制字段数量、嵌套深度、数组长度、字符串长度、节点数量
与 16,000 字节数据上限；本地观察元数据最多保留 10 个代码，订阅表最多 10 个代码。
`numbers.Integral/Real` 将原生数值标量转为 JSON 基础数值，内置端不导入 numpy/pandas。
数组仅接收 list/tuple；未知对象、非有限数值和超限载荷明确报错。

本模块不写行情文件、日志或数据库，也不把文件作为实时 IPC。用户显式要求的外部业务归档
由单独消费者处理。完整内存通道、实际快照字段、时区、持续更新、重连、退订和混合负载
的终端门禁，未执行时均为 `incomplete`。
