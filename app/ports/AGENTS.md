# app/ports 目录指南

## 一、目录用途

声明业务用例依赖的能力端口与跨层领域类型，不包含具体平台 IO。当前包含 M0 设备探针、M1 状态仓储、M3 层级/截图采集/共享设备门禁端口、M4a 策略文件/恢复活动状态端口、M4b 决策桥/当前包快照/可信租约/评测文件端口与 M4c profile 库/固定 OS 共享门禁端口；M2 `sense` 直接消费同一 `StorePort`，M3 `read` 不调用仓储。

## 二、当前文件

- `__init__.py`：声明端口包。
- `device.py`：`DeviceEntry`（逻辑别名与状态）、`ProbeSnapshot`（清单、选择、解锁、前台）以及 `DeviceProbe` 协议。
- `screen.py`：`HierarchyCapture`（XML 内存文本、受控证据名、字节数）、`ScreenCapture`（原始 RGBA 内存字节、受控证据名）、`ScreenReadError`（仅 `device_disconnected/invalid_fixture`）、`ScreenPort`（dump_hierarchy/capture_screen）与 `DeviceGate`（require_available/verify/release）/`DeviceGateUnavailable`。端口不暴露 serial、绝对路径或原始异常。
- `device_gate.py`：M4c `DeviceGateError`（`device_gate_unavailable/device_gate_io_error/device_busy/device_not_held/device_gate_mismatch/device_quarantined`）、`DeviceSessionIdentity` 与 `SharedDeviceGatePort`（acquire/verify/release/inspect/session_identity/recover）；`recover` 只在原 profile/原库绑定一致、显式确认与原 PID 已停止时受控接管 held/quarantined 占用，返回新 nonce；正常 acquire 不做 TTL/PID 自动解锁，`session_identity` 一次复核并返回 profile/原库/owner/nonce/held 完整身份，不暴露设备序列号或公开路径。真实 SQLite 排他竞争时 acquire 零等待返回 `device_busy`；verify/release/recover/session_identity 返回 `device_busy`，只读 inspect/inspect_gate_occupancy 返回保守 `device_gate_io_error`；内部 busy 分类不越过端口，也不释放或改写占用。
- `profile.py`：M4c `ProfileError`（`invalid_profile/invalid_fact/fact_conflict/fact_not_found/profile_io_error/profile_mismatch`）与 `ProfileStorePort`（open/set_fact/revoke_fact/forget_fact/active_fact_rows/source_ids_for_key/pending_activity/close）；只暴露本机 profile 存储与只读活动统计，不暴露绝对路径或事实值到日志。
- `store.py`：`StorePort` 协议与 `StoreError`；`begin_action` 可选独立 `apply_allowance`；方法为 `upsert_snapshot/claim/start/renew/finish/recover/unresolved_conversations/current_lease_expiry(lease, now)`（复用 `validate_lease` 并匹配 app/key/owner/nonce 与当前行真实截止）、`require_running`、`task_conversation`、M5 `begin_action/complete_action/freeze_attempted_action/reconcile_action/get_action/pending_actions/text_admission_status/load_ime_state/save_ime_state`；profile 模式 `upsert_snapshot` 在写事务前核对快照 profile_id，错误码固定为 `invalid_input/lease_busy/stale_lease/owner_not_stopped/clock_changed/store_busy/store_io_error/db_version_unsupported/migration_failed/action_conflict/action_not_found/action_not_ready/quota_exceeded/cooldown_active/text_send_blocked/invalid_action_key/invalid_observation/scene_unsettled/recovery_not_confirmed`。`sense` 只调用 `upsert_snapshot` 与 `unresolved_conversations`，`upsert_snapshot` 的返回值限定为写后仍 queued 的发现键；M3 `read` 不调用本端口。
- `actions.py`：M5 `ActionPlan`（动作键/类型/目标/会话、正文/附件/标签/IME/点击点、不可逆点击序号与完整身份标签，正文与坐标只在内存）、`ActionExecutionError(reason_code, clicked, ...收敛标志)`、`DeviceActionPort.read_current_ime/execute(*, before_command)`；端口在每条设备命令前调用调用方条件复核，只返回可信观察，不接收或宣告 Evidence，不重试。
- `policy.py`：M4a `PolicyError(reason_code, issues, *, activity_state)`、`PolicyRepositoryPort`（base/示例/活动包/快照/指南/preview 读取、暂存、原字节写、同卷发布、受控清理）与 `PolicyActivityPort.assess_restore(*, profile_id, dest_pack)->stopped|active|unknown`。原因码固定 `invalid_input/invalid_policy/policy_exists/policy_io_error/policy_changed/unknown_case/invalid_provider/eval_failed/authorization_required`；M4c 生产注入真实活动/未决检查；`RefusingPolicyActivity` 仅保留为受控拒绝实现。
- `decision.py`：M4b `BridgeDescriptor`、`BridgeError(reason_code, *, submitted)`、`DecisionBridgePort.complete(*, system_message, developer_message, user_message, echo)->str`、`DecisionSnapshotPort.reload()->(base,pack)`、`DecisionLeasePort.current_expiry()->str|null` 与 `DecisionClockPort.now()->str`；固定 `BRIDGE_TIMEOUT_SECONDS=20`、`BRIDGE_MAX_OUTPUT_BYTES=64KiB`、三角色、A/B 协议与 `bridge_start_failed/unavailable/timeout/protocol/roles/auth/quota/disconnected` 诊断。只有 `bridge_start_failed` 在传输层证明进程未启动时可重试；端口不暴露 argv、端点或凭据。
- `eval.py`：M4b `EvalStorePort`（公开/回归/留出套件读取、本机报告/凭证原子写入、留出登记锁内更新、无上传合同读取、退役 regression 准入写入）；不存在套件返回 null，路径/链接/坏格式映射固定策略错误。
- `runtime.py`：M6 `RuntimeScenePort`（返回 `read_fixture`、原始现场 `RawScene` 与合成 bridge 响应）、`RuntimeReadPort`（经真实 M3 解析返回 ReadResult，并用 `take_device_calls` 报告实际读取次数）、`SceneStep` 不再携带权威 task_outcome/read_result/decision；`RuntimeStorePort` 增加 resume/complete/checkpoint 证据、最近动作状态、草稿关联查询、语义阻塞与运行租约复核。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、边界

- 端口只暴露能力与领域字段，不暴露真实序列号、窗口原文、控件坐标、XML 节点、SQL 参数或数据库路径。
- 不导入 `adapters/`；实现由具体适配器提供。`store.py` 只导入 `domain/` 模型。
- M0 探针字段与原因码沿用实施计划；fixture 通道不启动子进程。
- `ScreenPort` 只做一次层级/截图采集；`DeviceGate.require_available` 必须在任何真实采集前通过，`verify` 在每条设备子命令前复核；M4c 生产门禁已交付，但真实 read CLI 仍未暴露，profile 内 primary 任务锁不能替代。
- `StorePort` 的租约、三态与 blocked 设备处置以设计说明 §6 为准；M1 只允许 app=boss。
- `PolicyRepositoryPort` 只暴露项目内文件能力与被校验包文本，不暴露绝对路径、正文或远程下载；`PolicyActivityPort` 只有 `stopped` 才允许 restore，`unknown/active` 均拒绝且不得用占位 True。
- `DecisionBridgePort` 只接收已渲染三分区消息与独立协议回显；`DecisionSnapshotPort`/`DecisionLeasePort`/`DecisionClockPort` 只提供当前包原文、可信截止时间与当前时间，不把模型输出、argv 或桥自报状态当授权。
- `EvalStorePort` 只读写项目内合成套件与被忽略的 `data/evals`，不读取真实私有样本内容到公开报告，不下载或上传；`read_acceptance` 只读固定运行目录凭证。
- `SharedDeviceGatePort` 只允许一个本机 primary 占用；不存在 TTL/PID 自动释放接口，verify/release 必须比较取得时的完整 owner/profile 身份，`recover` 仅用于原 profile/原库的受控对账接管。`ProfileStorePort` 只按 profile 固定库读写事实/元数据并只读现有 M1 状态，不预建 M5 动作表；真实任务租约由 `SqliteStore.current_lease_expiry` 与 `ProfileSessionLease` 组合交付，不使用固定测试租约。
- `DeviceActionPort` 只接收已渲染/已校验的计划并返回观察；不持有授权、额度、任务状态或 Evidence 判定，不自行重试。
- M6 端口不暴露正文、坐标、旧 nonce 或授权；场景/决策刺激只用于同一调度/存储/读取/决策/动作流程，不替代真实设备或 provider 标定。

## 四、维护要求

1. 修改端口字段或协议时，同步更新根 `AGENTS.md`、对应 usecase/adapter 指南和测试。
2. 新增端口必须有真实用例消费；不为未来可能性添加空接口。
3. 新增文件时同步本索引；每个文件顶部写用途，公开方法写 docstring。

## 五、已知限制

- `StorePort` 当前只有 SQLite 实现；策略包有文件适配器，M4c 已提供真实活动/未决检查与固定 OS 共享门禁适配器，M5 已提供 profile v2 动作账本与受控对账适配。M4b 决策桥与快照使用 A/B 子进程适配器、唯一 `RepositorySnapshot` 和测试注入；M4c 提供真实 profile 任务/共享门禁租约 `ProfileSessionLease`；M5 真实 ADB 执行器只由 mock 验证，CLI 未接真实动作入口。
- 真实适配器只有探针、SQLite 仓储、M3 层级/截图采集、M4a 策略文件与 M4b A/B 子进程桥；M3 不包含真实导航、点击、输入、滚动或截图分析，视觉端口未实现；M4b 不执行动作、不写事实、不连接设备。
