# app 目录指南

## 一、目录用途

承载 M0 起的业务程序。当前已实现 `python -m app` 入口、只读 `doctor` 探针与 M1 本机状态库（`store init` 和 SQLite 仓储）；按“CLI 组合根 → usecase → port ← adapter”的依赖方向组织，`domain/` 只放无 IO 模型与校验。M2+ 的传感器、决策、动作与调度尚未实现。

## 二、当前文件与子目录

- `__init__.py`：声明 `app` 包。
- `__main__.py`：`python -m app` 的进程入口，调用 `app.cli.main.main`。
- `cli/`：参数解析、配置校验、路径边界、JSON 输出与组合根；见 `cli/AGENTS.md`。
- `domain/`：M1 纯领域模型、键/摘要算法与无 IO 校验；见 `domain/AGENTS.md`。
- `ports/`：能力端口与领域快照/租约类型；见 `ports/AGENTS.md`。
- `usecases/`：单次用例的校验、唯一业务结果组装与判定；见 `usecases/AGENTS.md`。
- `adapters/`：具体平台通道；见 `adapters/AGENTS.md`。
- `process_log.py`：每次运行创建独立的固定字段本机 JSON Lines 过程日志，默认写到被忽略的 `data/logs/m0-process-<运行 UUID>.jsonl`；显式路径也只作命名基础，不共享追加。写失败或白名单拒绝都设置降级标记并返回 `False`，不抛出、不打印原值；唯一 `emit_degraded_warning` 每次运行只写一次固定 stderr 标记，CLI 与仓储 API 共用。阶段已扩展到 `store`，并以 `operation` 记录 init/open/upsert/claim/start/renew/finish/recover/unresolved_conversations，仍复用唯一日志器。

## 三、依赖与边界

1. `domain/` 只依赖标准库与自身，不导入 `adapters/`、`sqlite3`、`adb` 或具体应用控件。
2. `ports/` 只依赖标准库、自身类型与 `domain/` 模型，不导入 `adapters/`。
3. `usecases/` 依赖 `ports/` 与领域对象，不导入具体适配器。
4. `adapters/` 实现端口，不写调度、不决定策略档位；SQLite 适配器只写本机业务库，ADB 适配器只做显式探针。
5. `cli/main.py` 是当前唯一组合根；只有它可以把 fixture 输入、ADB 适配器或 SQLite 初始化注入用例。
6. M0 的 fixture 模式必须零设备、零网络、零子进程；CLI 可以导入 ADB 适配器模块，但导入本身必须零副作用，fixture 分支不得实例化或调用探针。真实 ADB 适配器只能在显式 `--device --serial` 下调用，且只能在测试中 mock，不在离线验收中执行。
7. M1 的 `store init` 不连接设备、不调用网络或模型，不读取旧库/根 `config.json`；只接受项目内相对 `--db`，已存在 v1 库只核验不改行。
8. 不读取根 `config.json`、环境文件、凭据或个人事实；不引入 Python 第三方依赖。
9. 过程日志是尽力而为的辅助产物：只追加固定字段，不写参数原值、serial、窗口原文、DB 绝对路径、异常或个人事实；入口忽略单次 `record` 的 `False`，但写失败或白名单拒绝会置 `degraded`，由 CLI 在 stderr 输出固定降级提示，不能声称日志完整。JSON 输出统一记录 `output` 成功/失败；ADB 命令与 store 调用记录 `started` 后再记 `ok/failed`。

## 四、测试与验证

在项目根执行：

```powershell
python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_process_log_concurrency
python -m app doctor --fixture tests/fixtures/doctor/ready.json
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

`tests.test_doctor` 覆盖 M0 CLI、配置与夹具校验、设备解析、日志与 fixture 零子进程；`tests.test_process_log_concurrency` 用两个独立进程验证默认与显式日志路径的隔离和完整关联；`tests.test_store` 覆盖候选入库、竞争、TTL、三态收敛、恢复、clock_changed、严格输入与事务回滚；`tests.test_migrations` 覆盖空库建表/核验、幂等、初始化回滚、schema 篡改与 `store init` CLI。真实 ADB 模式只通过 mock 的 subprocess 输出验证，不代表已在真机运行；M1 测试只用合成库与注入检查器。

## 五、维护要求

1. 新增、删除、改名或移动本目录文件/子目录时，同步更新本文件和根 `AGENTS.md`。
2. 新增承载真实代码的子目录时，同目录创建 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`；不创建空目录。
3. 每个源文件顶部说明用途，每个函数/方法写 docstring；修改模块时同步对应子目录指南。
4. 新增里程碑能力时先确认计划路径、`app/` 分层和测试入口，再按对应 M 的验收契约实现，不提前创建空模块。
5. 涉及真实设备、外部模型或网络时先取得本轮确认；当前不提供这些调用的默认入口。

## 六、已知限制

- M1 只实现设计说明 §6 的五表 v1、任务租约与 `store init`；M2 的 XML 解析/未读聚合、M3+ 的读取阶梯与设备动作尚未创建。
- 真实 `adb devices` / `dumpsys window` 未在本机执行，真实机型、系统版本、序列号格式与窗口输出差异未验证。
- `doctor` 只做一次独立探针，不提供业务循环、导航、截图或动作；`store init` 不做真实业务调度。
- M1 的 v1 只允许 app=boss；第二应用启用时需后续版本迁移，不预建迁移框架。
- 过程日志没有轮转、保留期、远程汇聚或审计保证；写入失败时只能通过固定 stderr 降级标记和缺失行发现，磁盘失败时日志不完整。
