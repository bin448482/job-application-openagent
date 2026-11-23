# app/ports 目录指南

## 一、目录用途

声明业务用例依赖的能力端口与跨层领域类型，不包含具体平台 IO。当前包含 M0 设备探针、M1 状态仓储与 M3 层级/截图采集/共享设备门禁端口；M2 `sense` 直接消费同一 `StorePort`，M3 `read` 不调用仓储。

## 二、当前文件

- `__init__.py`：声明端口包。
- `device.py`：`DeviceEntry`（逻辑别名与状态）、`ProbeSnapshot`（清单、选择、解锁、前台）以及 `DeviceProbe` 协议。
- `screen.py`：`HierarchyCapture`（XML 内存文本、受控证据名、字节数）、`ScreenCapture`（原始 RGBA 内存字节、受控证据名）、`ScreenReadError`（仅 `device_disconnected/invalid_fixture`）、`ScreenPort`（dump_hierarchy/capture_screen）与 `DeviceGate`/`DeviceGateUnavailable`。端口不暴露 serial、绝对路径或原始异常。
- `store.py`：`StorePort` 协议与 `StoreError`；方法为 `upsert_snapshot/claim/start/renew/finish/recover/unresolved_conversations`，错误码固定为 `invalid_input/lease_busy/stale_lease/owner_not_stopped/clock_changed/store_busy/store_io_error/db_version_unsupported/migration_failed`。`sense` 只调用 `upsert_snapshot` 与 `unresolved_conversations`，`upsert_snapshot` 的返回值限定为写后仍 queued 的发现键；M3 `read` 不调用本端口。

## 三、边界

- 端口只暴露能力与领域字段，不暴露真实序列号、窗口原文、控件坐标、XML 节点、SQL 参数或数据库路径。
- 不导入 `adapters/`；实现由具体适配器提供。`store.py` 只导入 `domain/` 模型。
- M0 探针字段与原因码沿用实施计划；fixture 通道不启动子进程。
- `ScreenPort` 只做一次层级/截图采集；`DeviceGate.require_available` 必须在任何真实采集前通过，M4 共享门禁未交付时抛出 `DeviceGateUnavailable`，profile 内 primary 任务锁不能替代。
- `StorePort` 的租约、三态与 blocked 设备处置以设计说明 §6 为准；M1 只允许 app=boss。
- 不预建 `decision.py`、`policy.py` 等未实现里程碑的空端口。

## 四、维护要求

1. 修改端口字段或协议时，同步更新根 `AGENTS.md`、对应 usecase/adapter 指南和测试。
2. 新增端口必须有真实用例消费；不为未来可能性添加空接口。
3. 新增文件时同步本索引；每个文件顶部写用途，公开方法写 docstring。

## 五、已知限制

- `StorePort` 当前只有 SQLite 实现；决策桥、策略包与完整共享 OS 设备门禁端口尚未定义。
- 真实适配器只有探针、SQLite 仓储与 M3 层级/截图采集；M3 不包含真实导航、点击、输入、滚动或截图分析，视觉端口未实现。
