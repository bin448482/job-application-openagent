# data 目录指南

## 目录职责

本机运行产物预留目录，只提交目录说明。全局工作规则见[根目录 AGENTS.md](../AGENTS.md)。

## 当前结构

- `evals/`：promptfoo 自动生成的本机状态及逐次 JSON 报告，被 Git 忽略；属于运行产物，排除指导文档覆盖。
- `logs/`：M0/M1 自动追加的本机过程日志。当前唯一文件为 `logs/m0-process.jsonl`，JSON Lines 格式，被 Git 忽略；运行前不存在，首次 `python -m app doctor` 或 `python -m app store init` 时创建。
- `runtime/`：M1 本机 SQLite 业务库目录，例如验收命令生成的 `runtime/m1-acceptance.sqlite3` 或默认 `runtime/runtime.sqlite3`；被根 `.gitignore` 的 `/data/**` 与 `*.sqlite3` 覆盖，不提交 Git，也不在文档中记录真实业务内容。
- `AGENTS.md`：本目录职责、当前结构、接口、适用约束与维护要求。
- `CLAUDE.md`：仅含 `@AGENTS.md`，引用本目录指南。

## 入口与接口

evals/run.cjs 将 promptfoo 状态及逐次结果写入 data/evals，smoke 与 model 使用不同目录前缀。M0 的 `app/process_log.py` 将固定白名单事件追加到 `data/logs/m0-process.jsonl`：每行一个 JSON 对象，固定包含 `schema_version/ts/correlation_id/stage/result/reason_code/error_category/duration_ms/command/exit_code/mode/config_source/device_count`。同一 `correlation_id` 串联参数、配置、fixture/探针、两条 ADB 命令、业务判定、output 和出口；ADB 命令先记 `started` 再记 `ok/failed`。M1 的 `store init` 复用同一日志器，阶段固定为 `store`，只写 `started/ok/failed`、仓储原因码、`error_category=store` 与耗时。日志只写固定阶段、结果、原因码、错误类别与耗时，不写参数原值、serial、窗口原文、数据库路径、SQL 参数、异常、绝对路径或个人事实。日志是尽力而为的本机辅助产物：调用端忽略单次 `record` 的 `False`，写入失败或白名单拒绝时设置 `degraded`，由 CLI 在 stderr 写固定 `m0_process_log_degraded` 并保持 stdout JSON/退出码不变；程序不宣称磁盘失败时仍有完整日志。

M1 的 `python -m app store init --db <项目内相对路径>` 只在 `data/runtime/` 或其它项目内路径创建/核验设计说明 §6 的五表 v1 库；已存在完整 v1 库只核验、不改业务行。M1 事件表只追加白名单元数据，不保存聊天正文；不得把业务库、事件或日志内容复制到公开报告。旧 `data/jobs.sqlite3` 等旧项目库不导入、不读取。

## 适用约束

数据库、日志、契约、凭据和个人事实只留本机。遵守根 .gitignore 的 data/** 排除及双指导文件例外；`data/logs/` 与 `data/runtime/` 是自动生成目录，不补指导文档、不提交 Git。M0 fixture 模式仍零设备、零网络、零子进程、零业务库，只追加该本机过程日志；M1 `store init` 只写项目内合成/本机 SQLite，不连接设备、网络或模型。不新增日志平台、后台任务或业务队列。真实 ADB、模型和网络调用仍须另行确认。

## 验证方式

从项目根执行 `npm run eval:smoke` 验证评测报告生成；执行 `python -m app doctor --fixture tests/fixtures/doctor/ready.json` 后抽查 `Get-Content data/logs/m0-process.jsonl -Tail 20`，按 `correlation_id` 串联阶段；执行 `python -m app store init --db data/runtime/m1-acceptance.sqlite3` 两次确认固定成功 JSON，并用只读查询比较 `user_version` 与业务行未变。`git check-ignore data/logs/m0-process.jsonl data/runtime/m1-acceptance.sqlite3` 检查运行产物排除。日志故障注入测试应同时确认 stdout/退出码不变、stderr 仅出现固定降级标记且无 traceback/原值。CLAUDE.md 只能包含单行 `@AGENTS.md`，可有末尾换行。不执行已清理业务模块的旧测试。

## 已知限制

评测产物不是业务执行、实际记忆持久化或设备验证结果；M1 只验证本机合成库的 schema、租约与事务，不代表真实设备调度。M1 过程日志没有轮转、保留期、远程汇聚或审计保证，写入失败或白名单拒绝时只能观察到业务 JSON 正常、stderr 固定降级标记与日志缺行，不能据此认为日志完整；真实设备日志证据仍待取得确认后的实机运行。`data/runtime/` 中的合成验收库不是可公开分享的业务数据，也不得据其推断个人事实。

## 同步维护要求

新增、删除、改名、移动文件或子目录，或职责、入口、接口、约束、测试命令、已知限制变化时，在同一变更中同步更新本指南；涉及其他目录时，同时更新对应指南和受影响父目录索引。不因占位说明创建空模块目录。
