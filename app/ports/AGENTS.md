# app/ports 目录指南

## 一、目录用途

声明业务用例依赖的能力端口与跨层领域类型，不包含具体平台 IO。当前包含 M0 设备探针、M1 状态仓储、M3 层级/截图采集/共享设备门禁端口与 M4a 策略文件/恢复活动状态端口；M2 `sense` 直接消费同一 `StorePort`，M3 `read` 不调用仓储。

## 二、当前文件

- `__init__.py`：声明端口包。
- `device.py`：`DeviceEntry`（逻辑别名与状态）、`ProbeSnapshot`（清单、选择、解锁、前台）以及 `DeviceProbe` 协议。
- `screen.py`：`HierarchyCapture`（XML 内存文本、受控证据名、字节数）、`ScreenCapture`（原始 RGBA 内存字节、受控证据名）、`ScreenReadError`（仅 `device_disconnected/invalid_fixture`）、`ScreenPort`（dump_hierarchy/capture_screen）与 `DeviceGate`/`DeviceGateUnavailable`。端口不暴露 serial、绝对路径或原始异常。
- `store.py`：`StorePort` 协议与 `StoreError`；方法为 `upsert_snapshot/claim/start/renew/finish/recover/unresolved_conversations`，错误码固定为 `invalid_input/lease_busy/stale_lease/owner_not_stopped/clock_changed/store_busy/store_io_error/db_version_unsupported/migration_failed`。`sense` 只调用 `upsert_snapshot` 与 `unresolved_conversations`，`upsert_snapshot` 的返回值限定为写后仍 queued 的发现键；M3 `read` 不调用本端口。
- `policy.py`：M4a `PolicyError(reason_code, issues, *, activity_state)`、`PolicyRepositoryPort`（base/示例/活动包/快照/指南/preview 读取、暂存、原字节写、同卷发布、受控清理）与 `PolicyActivityPort.assess_restore(*, profile_id, dest_pack)->stopped|active|unknown`。原因码固定 `invalid_input/invalid_policy/policy_exists/policy_io_error/policy_changed/unknown_case`；M4a 生产只注入返回 `unknown` 的受控实现，M4c 才接真实活动/未决检查。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、边界

- 端口只暴露能力与领域字段，不暴露真实序列号、窗口原文、控件坐标、XML 节点、SQL 参数或数据库路径。
- 不导入 `adapters/`；实现由具体适配器提供。`store.py` 只导入 `domain/` 模型。
- M0 探针字段与原因码沿用实施计划；fixture 通道不启动子进程。
- `ScreenPort` 只做一次层级/截图采集；`DeviceGate.require_available` 必须在任何真实采集前通过，M4 共享门禁未交付时抛出 `DeviceGateUnavailable`，profile 内 primary 任务锁不能替代。
- `StorePort` 的租约、三态与 blocked 设备处置以设计说明 §6 为准；M1 只允许 app=boss。
- `PolicyRepositoryPort` 只暴露项目内文件能力与被校验包文本，不暴露绝对路径、正文或远程下载；`PolicyActivityPort` 只有 `stopped` 才允许 restore，`unknown/active` 均拒绝且不得用占位 True。
- 不预建 M4b 模型桥、M5 动作或 M6 调度端口。

## 四、维护要求

1. 修改端口字段或协议时，同步更新根 `AGENTS.md`、对应 usecase/adapter 指南和测试。
2. 新增端口必须有真实用例消费；不为未来可能性添加空接口。
3. 新增文件时同步本索引；每个文件顶部写用途，公开方法写 docstring。

## 五、已知限制

- `StorePort` 当前只有 SQLite 实现；策略包有文件适配器，但 M4a 没有真实活动/未决检查实现，M4c 才注入；决策桥与完整共享 OS 设备门禁端口尚未定义。
- 真实适配器只有探针、SQLite 仓储、M3 层级/截图采集与 M4a 策略文件；M3 不包含真实导航、点击、输入、滚动或截图分析，视觉端口未实现；M4a 不调用桥、设备、网络或模型。
