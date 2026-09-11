# 内置 Python 通信能力清单（M1a）

这个目录的 `strategy_inventory.py` 是供用户手工加载到大 QMT 策略界面的单文件入口。
它只盘点内置 Python 的候选模块和 QMT 入口是否存在，不创建 IPC 对象，不订阅行情，
不读取账户，也不下单。

## 手工运行

1. 复制 `strategy_inventory.py` 的全部内容到 QMT 的策略编辑器；策略短名使用
   `MEMORY_INV`。
2. 在 QMT 外部终端生成一次未使用过的会话值：

   ```powershell
   $sessionId = python -c "import uuid; print(uuid.uuid4().hex)"
   ```

   将命令打印的 32 位小写十六进制值替换进 `SESSION_ID = ""`。这是一次性新鲜标识，
   不是认证凭据；不要把真实会话值或原始报告提交到公开仓库。
3. 取消 QMT 的“启动本地 Python”选项，然后由用户正常加载并运行策略。

脚本会在 `D:\bigqmt-data-bridge\evidence\memory-v1` 排他保存
`$sessionId.json`。看到 `QMT memory inventory saved:` 日志即表示本次清单已保存；
保存后策略自然结束是预期行为，不需要重跑同一会话。

在外部终端使用本机解释器校验报告：

```powershell
python experiments\memory_v1\inspect_report.py --report "D:\bigqmt-data-bridge\evidence\memory-v1\$sessionId.json" --session-id $sessionId
```

校验器退出码为 `0` 仅表示报告的固定清单格式有效，并且输出仍会明确写出
`"state":"incomplete"` 与 `"transport_verified":false`。它不会提升为跨进程通信
验证。某个模块或函数显示为不可用，也只说明这次内置解释器的候选入口缺失或异常，
并不等于整个项目最终不支持该能力。

本机一次实际清单的脱敏结论与后续门禁见
[2026-09-11-memory-inventory-live.md](../../docs/research/2026-09-11-memory-inventory-live.md)。
原始 JSON 是私有证据，不应复制、提交或公开。
