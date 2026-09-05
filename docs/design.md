# 独立开源拆分设计

## 已确认范围

将已有的只读文件桥提取为独立项目，不改原采集项目、正在运行的 QMT 策略和生产配置。不复制 Git 历史、私人配置、真实样本、券商源码或二进制。不上传 GitHub。

## 结构与接口

- `qmt_bridge/`：内置 Python 3.6 可解析的标准库服务端，沿用版本 1 文件协议和 worker 版本 2；无交易入口。
- `bigqmt_bridge/`：外部 Python 3.10+ 客户端、规范化、独立 JSON 配置和 `python -m bigqmt_bridge` 命令入口。
- `bigqmt_bridge/schemas/financial.json`：随包分发的八表字段契约；从已有接口合同提取字段名称，不包含数据库表或数据；不宣称券商支持所有字段。
- `tests/`：提取现有协议/客户端/对比测试，移除原采集项目集成测试，补独立配置、入口、资源加载测试。
- `skills/bigqmt-data-bridge/SKILL.md`：可复制安装的智能体参考技能；不假设技能与源码安装在同一位置。
- 中文 README、配置示例、MIT 许可证、发布清单。

仅提供文件桥。原分支的第三方 RPC 试验不作为本独立项目依赖；原生 xtquant 仅用于可选的只读基线取样，不自动回退。

## 使用契约

显式 `--config` 读取扁平 JSON 对象，不搜索原项目配置，不读取原项目环境变量。示例 `cache_prepared=false`；用户核对本地缓存后才能置 true。命令行参数优先于配置；无参数时 backend=file_bridge、probe timeout=5、sample timeout=60。

保留 subscribe=False、fill_data=False、空/残缺结果显式失败、完整合约字段门禁、指数成分映射门禁。下载兼容入口不实际下载；性能和真实客户端兼容性未验收的部分明确标注实验性。

## 验证

在独立目录运行全部测试；模拟 ContextInfo 仅证明本项目逻辑。构建 wheel 并在脱离源码目录时验证 CLI 和财务资源加载；检查内置端 Python 3.6 语法和标准库依赖。敏感信息及打包内容审查。技能分别进行无指导和有指导的只读应用演练，验证实际命令及限制判断。
