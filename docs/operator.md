# M0 只读探针与 M1 状态库操作手册

> 文件用途：说明当前已实现的 M0 `doctor` 与 M1 `store init` 能做什么、怎么运行、如何解读 JSON，以及在执行真实设备探针前必须完成的确认与边界；M2 及以后的能力尚未实现。

## 一、当前可用范围

已实现两条离线入口：

- `doctor`：一次独立的只读探针，读取 ADB 设备清单或合成 fixture，并输出设备数量、所选逻辑别名、解锁状态和前台应用类别。
- `store init`：创建或核验设计说明 §6 的本机 SQLite v1 状态库（五表、约束、索引与用户版本 1），不导入旧库。

M0/M1 都不切换应用、不 dump 聊天、不截图、不点击、不发送、不调用网络或模型。`fixture` 模式只读取合成 JSON，永远不启动子进程；`store init` 只写项目内本机 SQLite。每次 `doctor`/`store init` 运行只追加被忽略的 `data/logs/m0-process.jsonl` 本机过程日志（见第八节）。

M1 的仓储 API 已实现候选入库、任务租约、三态收敛与过期恢复，但 M1 **没有** `sense`、业务循环、设备动作或调度子命令；当前只通过单元测试使用合成 ReadResult/Evidence。

## 二、离线 fixture 模式（默认推荐）

在项目根目录执行：

```powershell
python -m app doctor --fixture tests/fixtures/doctor/ready.json
```

成功时退出 `0`，向 stdout 写一个 UTF-8 JSON 对象；本机日志正常时 stderr 为空（日志降级时只出现固定 `m0_process_log_degraded` 一行）：

```json
{"schema_version":1,"status":"ok","reason_code":null,"device_count":1,"selected_alias":"primary","unlocked":true,"foreground_app":"boss"}
```

该模式不要求安装 ADB，也不要求连接手机。`tests/fixtures/doctor/ready.json` 的内容是纯合成值，不是真实设备信息。

## 三、最小配置

默认配置等价于：

```json
{"schema_version":1,"mode":"read_only"}
```

不需要配置文件。只有在明确传入全局 `--config <相对文件>` 时才读取配置，且不会自动读取根目录 `config.json`。配置规则：

- 只接受 `schema_version` 与 `mode` 两个键；
- `schema_version` 只能是整数 `1`；
- `mode` 只能是 `read_only`；
- 缺键补默认值；空对象 `{}` 与省略配置等价；
- 额外键、`null`、布尔或其他类型、读取失败、绝对路径或越界路径都退出 `2` 并输出 `invalid_config`。

示例：

```powershell
python -m app --config <相对配置文件> doctor --fixture tests/fixtures/doctor/ready.json
```

`config.example.json` 当前只描述未来 M4 的 `policy_pack` 引用，不是 M0 可消费的最小配置，不应传给 `doctor`。

## 四、M1 状态库初始化（`store init`）

首次创建或重复核验：

```powershell
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

成功时退出 `0`，只向 stdout 输出固定对象：

```json
{"schema_version":1,"status":"ok","reason_code":null}
```

规则：

- `--db` 省略时为 `data/runtime/runtime.sqlite3`；
- 路径只接受项目内相对路径；解析符号链接和 `..` 后仍须位于项目根内，否则 `invalid_input`；
- 空库首次初始化在一个写事务内创建五张 STRICT 表、全部约束/外键/索引并设置 `user_version=1`；中途失败整体回滚，不残留半套表；
- 已存在的完整 v1 库只核验结构，成功幂等且不改变任何业务行；未知版本、非空无版本库或结构被增删改（表、列、约束、索引）都拒绝，绝不覆盖或重建；
- 不导入旧项目库，不读取根 `config.json`、环境文件、凭据或个人事实；
- 只写本机 SQLite，不连接设备、网络或模型。

v1 结构摘要（以设计说明 §6 为准）：

| 表 | 用途 |
|---|---|
| `hr_conversations` | 会话身份摘要与可选的真实读全基线；M1 不写基线。 |
| `hr_discoveries` | M1–M2 的候选队列：会话键、指纹、四源 JSON、快照 ID、`opened=0` 与公共任务状态/租约列。 |
| `hr_messages` | 为后续读全消息预留；M1 不写入。 |
| `device_lease` | 跨任务唯一的 `primary` 设备占用行；`held` 或 `quarantined`。 |
| `events` | 只追加白名单元数据事件，不含正文。 |

公共任务状态为 `queued/leased/running/done/not_sent/failed/blocked/cancelled`。任务与设备共享同一 owner/pid/host/nonce/截止时间；租约 TTL 为 60 秒。M1 只允许 `app=boss`，第二应用需后续版本迁移，不预建迁移框架。

仓储行为（通过测试或未来用例调用，不在此手工作业）：

- `upsert_snapshot` 只接受 `ReadResult.status=ok`、非空快照 ID 的完整合成结果；未知行不落库，同指纹重放不追加事件，queued 可升级 `new_message→unread`。
- `claim` 在任一 `device_lease` 行存在（含过期或 quarantined）时返回 `lease_busy`；仅 TTL 到期不会释放占用。
- `start` 只允许 `leased→running` 且不延长截止时间；`renew` 同步延长任务与设备；旧 Lease、旧 nonce 或重复操作返回 `stale_lease`。
- `finish` 只允许 `running→done/not_sent/failed/blocked`，原因必须与读取/证据三态匹配。blocked 清空任务租约，但保留设备原 owner/pid/host/nonce/截止时间，仅把 `reservation_state` 置为 `quarantined`；下一次 claim 仍为 `lease_busy`。
- `recover` 仅在任务/设备身份与 nonce 匹配、已过期、`confirm_stopped=true`、本机 host 匹配且注入的停止检查器判定 `stopped` 后生效：leased 回 `queued` 并删除设备行；running 转 `blocked/recovery_requires_reconciliation` 并隔离设备。未过期返回 `lease_busy`，其余停止/主机不确定返回 `owner_not_stopped`。
- 墙钟与单调时钟增量相差超过 1 秒返回 `clock_changed`，不执行新动作。
- 所有写操作使用 `BEGIN IMMEDIATE`，任务、设备或事件任一行更新/插入失败都整体回滚。

`data/runtime/` 是自动生成的本机产物目录，被 Git 忽略；不要提交数据库、事件或日志。

## 五、显式真实设备模式

只有同时给出 `--device` 与 `--serial` 才启用真实探针：

```powershell
python -m app doctor --device --serial <serial>
```

执行前必须完成本轮设备操作确认，并向使用者说明：

1. 将运行两条只读 ADB 命令：`adb devices` 与 `adb -s <serial> shell dumpsys window`；
2. 每条命令超时 `5` 秒，合计预计不超过 `10` 秒；
3. 探针期间独占设备，不并行运行其他设备用例；
4. 不切换应用、不读取聊天、不截图、不写入业务库；
5. 序列号只在本机内存中精确匹配，不写入输出；输出中的逻辑别名固定为 `primary`。

按计划 M0–M2 没有业务设备循环；当前仅 M0 探针与 M1 本机状态库已实现，真实探针只是一次独立调用。M3 开始前必须把真实探针纳入设备用例的全局占用，未接入时不得与设备循环并用。当前仓库尚未对任何真实设备执行过该模式。

## 六、输出与退出码

`doctor` 业务结果固定包含以下七个键：

| 键 | 含义 |
|---|---|
| `schema_version` | 固定整数 `1` |
| `status` | `ok` 或 `failed` |
| `reason_code` | 成功为 `null`，失败为固定原因码 |
| `device_count` | 设备清单条数 |
| `selected_alias` | 所选逻辑别名或 `null` |
| `unlocked` | `true`/`false` 或 `null`（未知） |
| `foreground_app` | `boss`、`other` 或 `unknown` |

`doctor` 业务失败原因码：

| 原因码 | 含义 |
|---|---|
| `device_disconnected` | 无设备、ADB 缺失、超时、非零退出或 serial 不在清单 |
| `device_ambiguous` | 多台设备且未选择 |
| `device_offline` | 设备离线或清单状态未知 |
| `device_unauthorized` | 设备未授权 |
| `device_locked` | 锁定或解锁状态未知 |
| `wrong_page` | 前台不是 BOSS 或无法判定 |

参数、配置、夹具或 M1 仓储错误不输出业务字段，而是输出三键安全对象：

```json
{"schema_version":1,"status":"failed","reason_code":"invalid_input"}
```

输入类原因只取 `invalid_input`、`invalid_config`、`invalid_fixture`；M1 仓储原因取 `invalid_input`、`lease_busy`、`stale_lease`、`owner_not_stopped`、`clock_changed`、`store_busy`、`store_io_error`、`db_version_unsupported`、`migration_failed`。所有失败退出 `2`；成功退出 `0`；帮助退出 `0`。程序不使用退出 `1` 表示可重试失败。

若业务 JSON 的 stdout 写入失败（`UnicodeError`/`OSError`），程序安全退出 `2`，不回显 traceback，也不要求再次写 JSON；日志会记录 `output=failed` 并以 `internal` 收尾。`--help` 仍由 argparse 直接处理、正常退出 `0`，不套用业务 JSON 契约。本机日志降级不是业务失败，退出码仍按业务结果；stderr 只多一行固定 `m0_process_log_degraded`。

## 七、fixture 输入规则

`--fixture` 指向的合成 JSON 必须恰好包含：

```json
{
  "devices": [{"alias": "primary", "state": "device"}],
  "selected_alias": "primary",
  "unlocked": true,
  "foreground_app": "boss"
}
```

- `alias` 是唯一、非空的逻辑名；
- `state` 只能是 `device`、`offline`、`unauthorized`；
- `selected_alias` 必须是已存在的别名或 `null`；
- `unlocked` 是布尔值或 `null`；
- `foreground_app` 只能是 `boss`、`other`、`unknown`；
- 任何额外键、重复别名、不存在的选择、非法类型或读取失败都输出 `invalid_fixture`。
- 配置与夹具在共享 JSON 输入边界拒绝顶层或嵌套对象的重复键；夹具还拒绝不可 UTF-8 编码的 `alias`/`selected_alias`（例如孤立代理字符），按 `invalid_fixture` 安全退出且不回显键值。

M1 的 ReadResult/Evidence 只由测试或后续 `sense` 构造；当前没有 CLI 文件入口，不要把 M0 fixture 形状当作 M1 快照输入。

## 八、本机过程日志与复核

每次 `python -m app doctor` 或 `python -m app store init` 运行都会尝试向 `data/logs/m0-process.jsonl` 追加 JSON Lines 过程日志。该路径被根 `.gitignore` 的 `/data/**` 覆盖，不提交 Git；不调用网络、不启动后台任务，也不写远程日志。

每行固定包含 `schema_version/ts/correlation_id/stage/result/reason_code/error_category/duration_ms/command/exit_code/mode/config_source/device_count`：

- `stage` 固定为 `start/arguments/config/mode/fixture/probe/adb_devices/adb_window/decision/store/output/exit`，可用同一 `correlation_id` 串联参数、配置、fixture/探针、两条 ADB 命令、业务判定、store 初始化/核验、输出和出口。
- `result` 固定为 `started/ok/failed`；`reason_code` 沿用计划词表，不新增业务原因；`error_category` 固定为 `argument/config/fixture/probe/business/store/internal/adb_missing/adb_timeout/adb_nonzero/adb_oserror`。
- `command` 只写 `adb_devices` 或 `adb_window`；每次命令先记 `started`，完成后再记 `ok` 或固定失败类别。
- `store` 阶段先记 `started`，再记 `ok` 或带固定仓储原因与 `error_category=store` 的 `failed`；不写数据库路径。
- `output` 只为 JSON 收尾：写出成功记 `ok`，写出失败记 `failed`（`error_category=internal`）并安全退出 `2`；不能据此假称 JSON 已输出。
- 不写参数原值、serial、窗口原文、数据库路径、SQL 参数、异常消息、绝对路径或个人事实。
- 日志是尽力而为的辅助产物：调用端忽略单次 `record` 的 `False`，写入失败时设置内部 `degraded` 标记；CLI 在出口向 stderr 写固定 `m0_process_log_degraded`，stdout JSON 与退出码不变，也不伪造成功行。没有日志行不等于命令成功；该标记表示日志不完整，不能声称磁盘失败时完整日志已保存。

复核方式：

```powershell
Get-Content data/logs/m0-process.jsonl -Tail 20
git check-ignore data/logs/m0-process.jsonl data/runtime/m1-acceptance.sqlite3
```

stdout 不包含 `correlation_id`。需要按单次运行核对时，从本机日志中取该次命令时间附近的 `start`→`exit` 事件。fixture 模式仍保持零设备、零网络、零子进程和零业务库；`store init` 只写项目内本机 SQLite。

## 九、尚未实现

M2 的传感器与未读聚合、M3 的读取阶梯、M4 的决策契约、M5 的动作护栏以及 M6–M8 的调度、清理和灰度都尚未实现。M1 的仓储 API 也尚未接入业务 CLI 或设备循环。不要用 M0 的 fixture 结果或 M1 的合成库推断真实设备支持范围，也不要把合成 `can_send`、探针成功或 `store init` 成功当作发送授权。

## 十、安全与隐私

- fixture 只用合成别名与状态，不放真实序列号、聊天、联系人、公司、截图或绝对路径。
- 真实模式输出不含真实序列号、窗口原文或异常命令参数。
- M1 只保存摘要、键、状态与白名单事件，不保存聊天正文；数据库、事件、日志与截图只在本机保存，不提交 Git。
- 不读取根 `config.json`、环境文件、凭据或个人事实。
- 设备、日志、截图和评测产物只在本机保存，不提交 Git。
- 发现疑似敏感信息时按根 `AGENTS.md` 的“开源项目敏感信息告知”处理，不在对话或公开材料中复述原值。
