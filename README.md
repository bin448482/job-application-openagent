# job-application-openagent

面向开源维护的 Android 求职助手项目（重新设计中）。

## 当前状态

旧的业务源码、Skills 与业务离线测试已清理，项目正在重新设计。当前保留项目骨架，并已建立独立的 promptfoo 合成黄金测试集：

- `pyproject.toml`、`requirements.txt`：Python 业务元数据；暂不引入第三方 Python 依赖。
- `config.example.json`：可提交的示例配置，正式结构待设计。
- `data/`、`screenshots/`：本机运行产物目录，只提交目录说明。
- `.agents/AGENTS.md`、`.claude/AGENTS.md`：工作流入口占位，内容待设计。
- `AGENTS.md`、`CLAUDE.md`：开发约束与目录索引。
- `evals/`、`promptfooconfig.json`：24 个记忆与授权决策案例、评测提示词、断言和运行入口。
- `package.json`、`package-lock.json`：锁定 promptfoo 评测开发依赖，不改变 Python 业务依赖。

## 开发环境

- Python 3.13 或更高版本。
- 评测工具另需 Node.js >=22.22.0 与 npm；promptfoo 固定为 0.126.0。
- 真机功能另需 Android SDK Platform-Tools 中的 `adb`、USB 调试和使用者确认；离线测试不连接设备。
- 仓库中的 `.venv/` 与 `config.json` 为被 Git 忽略的本机文件，可自行保留或删除。

业务设计文档确定后再补充业务安装、入口和验证说明。数据库、日志、截图、契约、凭据和个人事实只在本机保存。

全新机器的引导、依赖边界与验收规划见[新环境自动化部署方案](docs/求职自动化_新环境自动化部署方案_20261123.md)：分别设计 eval、ADB 工具准备和经本轮确认后的手机环境检测（§8、§10.1）；工具安装不代表手机或业务就绪。安装器与手机检查器尚未实现，干净机器及手机验收均待执行；本轮仅文档，不运行 ADB 或连接手机。

## 黄金测试集

在项目根目录执行：

```powershell
npm ci
npm test
npm run eval:smoke
```

`npm test` 使用 Node 内置测试验证断言，包括错误输出反例。`eval:smoke` 通过参考 provider 回放合成答案，只验证 promptfoo 链路；两者均不请求模型、不连接设备，也不证明模型已通过黄金集。

评测真实模型时显式选择 promptfoo 支持的 provider，并按该 provider 的文档配置认证：

```powershell
npm run eval:model -- "<provider-id>"
```

该命令会将合成输入交给指定 provider，可能产生模型调用费用。当前没有默认真实模型，不能把 smoke 结果当作模型评测。结果只写到被 Git 忽略的 `data/evals/`；入口关闭遥测、更新检查、缓存及分享，不读取本机 config.json 或个人事实。

案例覆盖偏好有效性与冲突、长期更新/临时偏好/遗忘、授权范围与到期、动作防重、请求与交付区别及 HR 指令注入。所有地点、薪资和身份均为合成值。当前只给出记忆决策协议，SQLite 运行时记忆及端到端读写仍待实现。字段和限制见 [evals/AGENTS.md](evals/AGENTS.md)。

工具依据：promptfoo 官方 JavaScript assertion 与自定义 provider 文档。配置和执行已按固定版本在本机验证；case metadata 不进入模型提示词。

许可证：MIT，见 `LICENSE`。
