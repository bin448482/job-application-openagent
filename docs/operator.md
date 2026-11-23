# M0 只读探针操作手册

> 文件用途：说明当前已实现的 M0 `doctor` 能做什么、怎么运行、如何解读 JSON，以及在执行真实设备探针前必须完成的确认与边界；M1 及以后的能力尚未实现。

## 一、当前可用范围

M0 只提供一次独立的只读探针：

- 读取 ADB 设备清单；
- 读取当前窗口的 `isKeyguardShowing` 与 `mCurrentFocus`；
- 输出设备数量、所选逻辑别名、解锁状态和前台应用类别。

M0 不切换应用、不 dump 聊天、不截图、不点击、不发送、不写 SQLite 业务库、不调用网络或模型。`fixture` 模式只读取合成 JSON，永远不启动子进程；每次运行只追加被忽略的 `data/logs/m0-process.jsonl` 本机过程日志（见第七节）。

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

## 四、显式真实设备模式

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

按计划 M0–M2 没有业务设备循环；当前仅 M0 已实现，真实探针只是一次独立调用。M3 开始前必须把真实探针纳入设备用例的全局占用，未接入时不得与设备循环并用。当前仓库尚未对任何真实设备执行过该模式。

## 五、输出与退出码

业务结果固定包含以下七个键：

| 键 | 含义 |
|---|---|
| `schema_version` | 固定整数 `1` |
| `status` | `ok` 或 `failed` |
| `reason_code` | 成功为 `null`，失败为固定原因码 |
| `device_count` | 设备清单条数 |
| `selected_alias` | 所选逻辑别名或 `null` |
| `unlocked` | `true`/`false` 或 `null`（未知） |
| `foreground_app` | `boss`、`other` 或 `unknown` |

业务失败原因码：

| 原因码 | 含义 |
|---|---|
| `device_disconnected` | 无设备、ADB 缺失、超时、非零退出或 serial 不在清单 |
| `device_ambiguous` | 多台设备且未选择 |
| `device_offline` | 设备离线或清单状态未知 |
| `device_unauthorized` | 设备未授权 |
| `device_locked` | 锁定或解锁状态未知 |
| `wrong_page` | 前台不是 BOSS 或无法判定 |

参数、配置或夹具错误不输出上面的业务对象，而是输出三键安全对象：

```json
{"schema_version":1,"status":"failed","reason_code":"invalid_input"}
```

`reason_code` 只取 `invalid_input`、`invalid_config` 或 `invalid_fixture`。所有失败退出 `2`；成功退出 `0`；帮助退出 `0`。程序不使用退出 `1` 表示可重试失败。若 stdout 本身写入失败（`UnicodeError`/`OSError`），程序安全退出 `2`，不回显 traceback，也不要求再次写 JSON。本机日志降级不是业务失败，退出码仍按业务结果；stderr 只多一行固定 `m0_process_log_degraded`。

## 六、fixture 输入规则

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

## 七、本机过程日志与复核

每次 `python -m app doctor` 运行（fixture 或显式 device）都会尝试向 `data/logs/m0-process.jsonl` 追加 JSON Lines 过程日志。该路径被根 `.gitignore` 的 `/data/**` 覆盖，不提交 Git；不写业务库、不调用网络、不启动后台任务，也不与 M1 事件表重复。

每行固定包含 `schema_version/ts/correlation_id/stage/result/reason_code/error_category/duration_ms/command/exit_code/mode/config_source/device_count`：

- `stage` 固定为 `start/arguments/config/mode/fixture/probe/adb_devices/adb_window/decision/exit`，可用同一 `correlation_id` 串联参数、配置、fixture/探针、两条 ADB 命令、业务判定和出口。
- `result` 固定为 `started/ok/failed`；`reason_code` 沿用计划词表，不新增业务原因；`error_category` 固定为 `argument/config/fixture/probe/business/internal/adb_missing/adb_timeout/adb_nonzero/adb_oserror`。
- `command` 只写 `adb_devices` 或 `adb_window`；ADB 缺失、超时、其它 IO 错误和非零退出分别记录固定错误类别，业务 `reason_code` 仍为 `device_disconnected`。
- 不写参数原值、serial、窗口原文、异常消息、绝对路径或个人事实。
- 日志是尽力而为的辅助产物：调用端忽略单次 `record` 的 `False`，写入失败时设置内部 `degraded` 标记；CLI 在出口向 stderr 写固定 `m0_process_log_degraded`，stdout JSON 与退出码不变，也不伪造成功行。没有日志行不等于命令成功；该标记表示日志不完整，不能声称磁盘失败时完整日志已保存。

复核方式：

```powershell
Get-Content data/logs/m0-process.jsonl -Tail 20
git check-ignore data/logs/m0-process.jsonl
```

需要按单次运行核对时，从输出或日志中取同一 `correlation_id` 过滤；fixture 模式仍保持零设备、零网络、零子进程和零业务库。

## 八、尚未实现

M1 的状态库与租约、M2 的传感器、M3 的读取阶梯、M4 的决策契约、M5 的动作护栏以及 M6–M8 的调度、清理和灰度都尚未实现。不要用 M0 的 fixture 结果推断真实设备支持范围，也不要把合成 `can_send` 或探针成功当作发送授权。

## 九、安全与隐私

- fixture 只用合成别名与状态，不放真实序列号、聊天、联系人、公司、截图或绝对路径。
- 真实模式输出不含真实序列号、窗口原文或异常命令参数。
- 不读取根 `config.json`、环境文件、凭据或个人事实。
- 设备、日志、截图和评测产物只在本机保存，不提交 Git。
- 发现疑似敏感信息时按根 `AGENTS.md` 的“开源项目敏感信息告知”处理，不在对话或公开材料中复述原值。
