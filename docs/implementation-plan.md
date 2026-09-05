# Standalone BigQMT Bridge Implementation Plan

**Goal:** 提取可独立安装、可公开分享的只读桥，附中文 README 和智能体技能。
**Architecture:** 保留文件协议与双解释器边界；客户端改名并解除配置/SQL依赖；不部署到真实 QMT。
**Tech Stack:** Python 3.10+、pandas、setuptools、pytest；内置端 Python 3.6 标准库。
**Spec:** [design.md](design.md)。用户已确认本次拆分与技能附带范围。

## Global Constraints

- 原项目只读；不复制配置、真实数据、券商源码、Git 历史。
- 无下载/交易/自动生产切换；不得宣称兼容性全面通过。
- 新文件和改造使用 apply_patch；仅本地准备，不 push。

## Task 1: 独立客户端和打包

- [x] 在 `tests/test_standalone.py` 写配置、CLI、八表资源的行为测试。显式 JSON 配置必须生效，未知键和非布尔缓存标志必须报错，无配置不能读取外部项目设置。
- [x] 运行 `python -m pytest tests/test_standalone.py -q`，确认独立包缺失造成预期失败。
- [x] 提取 `qmt_bridge/*.py`、`bigqmt_bridge/{backend,transport,normalize}.py` 和 CLI，配置改为 `load_config(path=None)`，财务资源用 `importlib.resources`，仅启用 file_bridge。
- [x] 添加 `pyproject.toml`，运行迁移过的协议/规范化/对比测试。

## Task 2: 使用文档和智能体技能

- [x] 无技能基线：独立代理不能从项目名推断取样/对比命令；明确未知命令，不直接迁移全量或删锁。
- [x] 写 README 的部署、配置、Python调用、CLI命令、限制、排障、授权边界和发布步骤。
- [x] 写自包含技能，给出实际入口、显式路径/日期、参考配置、缓存确认、probe解释及失败处理。
- [x] 用同场景有技能演练验证命令与安全判断；运行技能格式验证器。

## Task 3: 交付验证

- [x] 全量离线测试；wheel 构建与离源码导入；运行 CLI help 与无 worker 的短超时失败测试。
- [x] 检查公开目录的文件清单、路径、凭据模式；核对原仓库未改动。
- [x] 记录验证结果，交付 README 和 SKILL 链接；不发布到 GitHub，不安装技能到个人目录。
