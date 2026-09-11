# 发布前检查（0.2.0a1 Alpha）

- [ ] README 清楚区分默认 `history_mode=cache_only`（不下载）和显式 opt-in 的 `history_mode=auto`；保留 Alpha、无交易、未完成正式新 worker 真实终端验收和不承诺生产可用等说明。
- [ ] auto 只开放股票/ETF/指数的已结束交易日 `1d`、`1m`、`5m`；没有把 Tick、财务、指数权重、订阅或交易写成已支持。
- [ ] `qmt_bridge/strategy.py` 与 cache_only 默认行为保留；auto 使用独立 `qmt_bridge/strategy_auto.py`、独立 `D:\bigqmt-auto-runtime`，且 `ENABLE_DOWNLOADS` 默认 false。
- [ ] 全部离线测试通过；源码模块及财务 JSON 可读取，CLI help 可执行。后续仅交付源码，不再构建安装包。
- [ ] CLI `download` / `download-status` 仅在聚合状态 `verified` 且 job-level `errors` 为空时返回 0；探测失败保留报告和自动生成的 job/cell ID，不伪造 item 状态；新一轮 refresh marker 从 probe 前持续到全部逐项复核结束；status 仅读 `client_jobs/`，不发 QMT 请求。
- [ ] 多日示例必须显式提供真实交易日 `--expected-dates`；unknown 处置必须保持同范围/同 ID，不能教用户换 ID 重发。
- [ ] 只发布本独立目录，不复制原采集仓库 `.git`、配置、SQL、输出、日志或客户端安装目录。
- [ ] 检查未跟踪和暂存文件：不包含账号、密码、token、webhook、IP拓扑、私人路径、证书和真实样本。
- [ ] 不仅检查 `.gitignore`：检查将提交的实际文件内容。忽略规则不能清除已经提交的秘密。
- [ ] 确认 `records/`、`states/`、`client_jobs/`、`response_repairs/`、请求/响应、日志、锁和证据没有进入 Git；`response_repairs/` 只被描述为有界修复标记，不是第二请求队列。
- [ ] MIT 版权声明保留；没有把券商源码和依赖包误标为 MIT。
- [ ] 单独确认行情数据使用/再分发许可；issue 演示用合成样本。
- [ ] Skill 本版仍为 legacy/cache_only，不让旧 Skill 静默教授 auto 下载语义；README 不包含智能体使用说明。
- [ ] 不承诺全 xtquant 兼容、财务八表通过、生产无缝迁移或已达全市场性能。
- [ ] 如曾在同一 QMT 进程导入 auto worker，不能把重新运行同一源码入口当成升级；安排安全客户端重启或版本化模块，并保留实际加载证据。
- [ ] 确认自己选择的 GitHub 仓库可见性、remote 地址及发布版本，然后再 push。

推送与主分支合并须分别核对目标仓库、分支和维护者的明确授权，不发布安装包。`dist/`、`build/` 是历史可忽略构建产物，不应一并提交。真实终端尚未完成的验收必须继续标为 pending。
