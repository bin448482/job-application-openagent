# job-application-openagent

面向开源维护的 Android 求职助手项目（重新设计中）。

## 当前状态

旧的业务源码、Skills 与离线测试已清理，项目正在重新设计。当前仓库只保留项目骨架：

- `pyproject.toml`、`requirements.txt`：项目元数据；重新设计前不声明任何依赖。
- `config.example.json`：可提交的示例配置，正式结构待设计。
- `data/`、`screenshots/`：本机运行产物目录，只提交目录说明。
- `.agents/AGENTS.md`、`.claude/AGENTS.md`：工作流入口占位，内容待设计。
- `AGENTS.md`、`CLAUDE.md`：开发约束与目录索引。

## 开发环境

- Python 3.13 或更高版本。
- 真机功能另需 Android SDK Platform-Tools 中的 `adb`、USB 调试和使用者确认；离线测试不连接设备。
- 仓库中的 `.venv/` 与 `config.json` 为被 Git 忽略的本机文件，可自行保留或删除。

设计文档确定后再补充安装、入口和验证说明。数据库、日志、截图、契约、凭据和个人事实只在本机保存。

许可证：MIT，见 `LICENSE`。
