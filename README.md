# job-application-openagent

面向开源维护的 Android 求职助手项目（重新设计中）。

## 当前状态

旧的业务源码、Skills 与业务离线测试已清理，项目正在分阶段实现。当前已实现 M0 只读探针与 M1 本机状态库/租约，并已建立独立的 promptfoo 合成黄金测试集：

- `pyproject.toml`、`requirements.txt`：Python 业务元数据；暂不引入第三方 Python 依赖。
- `app/`：M0/M1 分层程序，提供 `python -m app`、只读 `doctor` 与 `store init`，以及 `domain/` 纯模型和 `adapters/sqlite/` 本机仓储。
- `tests/`：M0/M1 标准库 `unittest` 与纯合成夹具；M2+ 测试待实现。
- `docs/operator.md`：M0 探针与 M1 状态库的运行、配置、设备确认、schema 与错误码说明。
- `config.example.json`：可提交的示例配置，当前只描述未来 M4 的策略包引用，不是 M0 配置。
- `data/`、`screenshots/`：本机运行产物目录，只提交目录说明；`data/runtime/` 中的 SQLite 库被忽略。
- `.agents/AGENTS.md`、`.claude/AGENTS.md`：工作流入口占位，内容待设计。
- `AGENTS.md`、`CLAUDE.md`：开发约束与目录索引。
- `evals/`、`promptfooconfig.json`：24 个记忆与授权决策案例、评测提示词、断言和运行入口。
- `package.json`、`package-lock.json`：锁定 promptfoo 评测开发依赖，不改变 Python 业务依赖。

## M0 只读探针（已实现）

在项目根目录执行：

```powershell
python -m unittest tests.test_doctor
python -m app doctor --fixture tests/fixtures/doctor/ready.json
```

第二条命令退出 `0`，只向 stdout 输出一个 UTF-8 JSON：

```json
{"schema_version":1,"status":"ok","reason_code":null,"device_count":1,"selected_alias":"primary","unlocked":true,"foreground_app":"boss"}
```

fixture 模式只读合成输入，不启动子进程、不连接设备、不调用网络、不写业务库；每次运行只创建自己的被忽略 `data/logs/m0-process-<运行 UUID>.jsonl` 并写固定字段过程日志，运行之间不共享追加，写入失败不影响 stdout 或退出码，但 stderr 会写固定 `m0_process_log_degraded` 表示日志不完整。配置只有在显式传入全局 `--config <相对文件>` 时读取；省略与 `{}` 都取 `{"schema_version":1,"mode":"read_only"}`，不会自动读取根 `config.json`。配置与夹具在共享 JSON 输入边界拒绝重复对象键（含嵌套），夹具还拒绝不可 UTF-8 编码的字符串，错误只输出安全 JSON 且不回显原值。

真实探针必须显式指定：

```powershell
python -m app doctor --device --serial <serial>
```

执行前必须说明两条只读命令、预计最多 10 秒、设备独占要求并取得本轮确认。输出不含真实序列号或窗口原文。当前尚未在真实设备上执行该模式，M0 也不提供业务循环、导航、截图或发送。完整规则见 [docs/operator.md](docs/operator.md)。

## M1 状态库与租约（已实现）

创建或核验本机 SQLite v1 库：

```powershell
python -m unittest tests.test_store tests.test_migrations tests.test_process_log_concurrency
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

第二条命令退出 `0`，只输出固定 JSON：

```json
{"schema_version":1,"status":"ok","reason_code":null}
```

连续执行两次都成功，第二次只核验结构、不改变业务行。`--db` 省略时为 `data/runtime/runtime.sqlite3`；只接受项目内相对路径，解析链接和 `..` 后越界即 `invalid_input`。首次初始化在一个事务内创建五张 STRICT 表（`hr_conversations`、`hr_discoveries`、`hr_messages`、`device_lease`、`events`）、全部约束、外键与索引并设置 `user_version=1`；中途失败整体回滚。已存在完整 v1 库不得覆盖重建；未知版本、非空无版本或结构篡改都拒绝为 `db_version_unsupported`。

M1 仓储已实现合成快照入库、`claim/start/renew/finish/recover`、全局 `primary` 设备租约与未关闭会话查询；只允许 `app=boss`。任务 TTL 为 60 秒，仅 TTL 到期不会释放设备占用；`blocked` 会清任务租约但保留设备原身份并置 `quarantined`，下一次 claim 仍为 `lease_busy`。恢复要求已过期、显式确认、本机 host 匹配且注入的停止检查器判定 `stopped`。所有写操作同事务，失败整体回滚。M1 没有 `sense`、业务循环、设备动作或调度，M2 的 XML 解析不在当前范围。完整 schema、状态机与错误码见 [docs/operator.md](docs/operator.md)；`data/runtime/` 被 Git 忽略，不得提交数据库或事件。

## 开发环境

- Python 3.13 或更高版本。
- 评测工具另需 Node.js >=22.22.0 与 npm；promptfoo 固定为 0.126.0。
- 真机功能另需 Android SDK Platform-Tools 中的 `adb`、USB 调试和使用者确认；M0/M1 离线测试不连接设备。
- 仓库中的 `.venv/` 与 `config.json` 为被 Git 忽略的本机文件，可自行保留或删除。

M2–M8 的业务入口、传感器、SQLite 运行时记忆扩展和设备循环仍待实现。数据库、日志、截图、契约、凭据和个人事实只在本机保存。

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
