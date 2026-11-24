# app/adapters/sqlite 目录指南

## 一、目录用途

实现 `app/ports/store.py` 声明的 M1 本机 SQLite 仓储与 M4c profile 事实库、固定 OS 共享设备门禁、restore 活动检查：空库 v1 建库/核验、候选入库、全局 primary 设备租约、任务 start/renew/finish/recover 与只读未关闭会话查询。不连接设备、不调用模型、不读取旧库。

## 二、当前文件

- `__init__.py`：声明 SQLite 适配器包。
- `schema.py`：五张 STRICT 表与五个索引的确定性 DDL；`initialize_database` 在单个 `BEGIN IMMEDIATE` 事务内建库或核验，任一步失败回滚；`connect_database` 打开现有 v1 库并设置 `foreign_keys=ON`、`busy_timeout=5000`；`verify_v1` 比较 `user_version`、对象集合与规范化 SQL，缺失、额外或篡改均报 `db_version_unsupported`。
- `profile_schema.py`：M4c profile 库完整对象集 = 复用 `schema.py` 的 v1 五表/索引 DDL + `profile_meta/profile_facts/profile_fact_events` 与索引；`initialize_profile_database` 在写事务内创建或核验完整结构与绑定 ID，`connect_profile_database` 拒绝旧 v1 库/篡改/身份不符；不复用 `verify_v1` 的精确五表断言，也不改 M1 五表合同或虚构 1→2 迁移。
- `profile_store.py`：`SqliteProfileStore` 固定 `data/runtime/profiles/<profile_id>/runtime.sqlite3`，打开前用 `probe_path` 区分 missing/file/other/unknown；权限或目录等不可判定状态 `profile_io_error`；`set_fact` 生成来源 ID、`replace` 显式作废旧 active、非 replace 允许冲突待办；`revoke_fact` 保留历史，`forget_fact` 删除事实行并只写无值来源审计；`active_fact_rows`/`source_ids_for_key`/`pending_activity` 只读。值只进本机库/交互输出，不写日志或错误。
- `device_gate.py`：`SharedDeviceGate` 固定 Windows `LOCALAPPDATA/job-application-openagent/device-guard.sqlite3`；唯一 primary、`BEGIN IMMEDIATE` 插入、held/quarantined 任意状态拒绝；acquire 保存完整 owner_id/pid/host/nonce/profile/路径，verify 与 release 写事务共用 `_matches_session` 完整身份复核，任一变化不删除原占用；只允许 user_version=0 且无用户对象的明确新空库初始化，已有库核验版本、完整对象集与 DDL，未知/额外/旧表不改写；目录/不可读/权限查询错误返回固定 IO 或 unknown，只有 `probe_path` 明确 missing 才无状态。正常收敛 `release` 删除、异常 `release(converged=False)` 置 quarantined；不做 TTL/PID/重启自动解锁。`session_identity` 只读取一次并对同一行完成完整取得时身份核验后构造身份，不再 verify 后二次裸读；verify 也复用同一行核验。`inspect_gate_occupancy` 只按固定路径读取占用行（缺失返回 None、不建库、不改写），`inspect` 复用同一只读核心。门禁适配器唯一记录 acquire/verify/release/quarantine 的 started/终态。测试可注入临时 OS 根/平台名，生产无自选路径。
- `activity.py`：`ProfilePolicyActivity.assess_restore` 只读检查共享门禁与 profile 库现有 M1 状态：held→active、quarantined/坏库/绑定不符/存在但非普通文件→unknown、leased/running→active、blocked→unknown、明确不存在才 stopped；不释放占用、不改库，也不声称已实现 M5 动作对账；profile 打开步骤写 started/终态日志。
- `session.py`：`ProfileSessionLease` 实现 `DecisionLeasePort.current_expiry`；每次调用比较 store.profile_id/store.path/调用方 profile 路径，经 `gate.session_identity()` 核对门禁 profile/原库/owner/自身 nonce 与取得时 `Lease.owner` 一致，再经 `SqliteStore.current_lease_expiry(lease)` 重核任务 app/key/nonce 与设备/任务两行 owner/pid/host、held 且未过期；跨 profile/原库/owner、复制库、成对 owner 变化、门禁 nonce 桥前/桥期间变化、错误任务 key 或 app 均返回 None，由 `decide` 桥前/桥后 `stale_decision` 拒绝且不重发。
- `repo.py`：`SqliteStore` 实现 upsert/claim/start/renew/finish/recover/unresolved_conversations/current_lease_expiry；可选 `profile_id` 只在完整 profile 库校验后由 `connect_profile_database` 打开，普通 M1 库仍走精确五表 `connect_database`；`current_lease_expiry(lease, now)` 先用既有 `validate_lease` 校验完整参数，再按 app/key/nonce 选择同一任务并重核任务与设备两行完整 owner/pid/host/nonce/到期等于取得时 Lease，不匹配返回 None；profile 模式 `upsert_snapshot` 在写事务前拒绝另一 profile 的合法快照。其余实现默认创建并可注入唯一 `ProcessLog`，打开库及公开操作以 `operation` 记录 started/ok/failed、固定原因码与耗时，不记 PID/host/nonce/键/正文/SQL/异常，业务事务与幂等不受影响；日志降级时由 ProcessLog 唯一告警出口最多提示一次，构造失败无实例时也可观察。`open_store` 边界先记录 started，再在连接前校验本机 host 摘要；非法 host 记 `invalid_input`/`store` 失败且不创建或改动数据库。默认本机 host 为 `H(platform.node())`，owner.id 必须是规范 UUID、owner.host 必须是 64 位小写十六进制本机摘要，租约 key 必须是当前 app 的 discovery 键。默认 Windows 停止检查器用 ctypes `OpenProcess`（同时请求查询权限与 `SYNCHRONIZE`）/`WaitForSingleObject`，显式声明 x64 HANDLE/DWORD/BOOL 签名；非 Windows 用 `os.kill(pid, 0)`。支持注入单调时钟、停止检查器与仅测试使用的故障点回调。

## 三、库结构与事务契约

- 表：`hr_conversations`、`hr_discoveries`、`hr_messages`、`device_lease`、`events`，全部 STRICT；app 固定 boss，业务键必须带应用前缀；外键 `ON DELETE RESTRICT`。
- `hr_discoveries` 复用公共状态列，租约五字段与 leased/running 联动；queued/leased/running/done 的 reason 必须为空，其余状态必须非空。
- 唯一约束/索引：会话 `(app,identity_hash)`；发现活跃会话唯一（queued/leased/running/blocked）、nonce 唯一（非空）；领取索引按 `priority DESC,created_at,key`；事件审计 `(app,subject_key,event_id)`。
- 空库初始化只在 `user_version=0` 且无用户对象时建表并设版本 1；已有完整 v1 只核验、不改业务行；未知版本、非空无版本或结构篡改拒绝，绝不覆盖重建。初始化/打开遇到另一连接持写锁且等待耗尽时映射 `store_busy`，非 SQLite/IO 才是 `store_io_error`。
- 会话键必须精确等于 `app:conv:identity_hash`；仓储入口拒绝伪造会话键与不匹配的发现键，整批回滚且不留下事务。
- 只读 `unresolved_conversations` 不写事务；遇到另一连接独占写锁且等待耗尽时同样映射 `store_busy`，释放锁后正常返回且业务行不变。
- 写入口均 `BEGIN IMMEDIATE`，失败整体回滚。`claim` 见任一 primary 设备行（含过期或 quarantined）即 `lease_busy`；任务与设备共享同一 owner/pid/host/nonce/截止时间。`finish` 与 running 的 `recover` 进入 blocked 时清任务租约，但保留设备原身份并只置 `quarantined`；TTL 到期不释放占用。
- 事件只写 `snapshot_upserted/candidate_superseded/candidate_deferred/task_claimed/task_started/lease_renewed/task_finished/lease_recovered`，payload 固定 `from_state/to_state/nonce/candidate_count`，不写正文。
- 所有入口严格校验类型、枚举、UUID、摘要与 UTC；bool 不冒充整数；跨 app 键与非 discovery 租约键拒绝；枚举集合校验前先做类型检查，不把 `TypeError` 当成合法输入错误。仓储错误只抛 `StoreError` 固定原因码，不输出 SQL 参数或原始异常。

## 四、测试

```powershell
python -m unittest tests.test_store tests.test_migrations tests.test_profile_facts tests.test_device_gate tests.test_m4c_closed_loop
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

`tests.test_store` 覆盖竞争、TTL、停止未知/异主机、旧 nonce/截止时间、重复 start/finish、blocked 设备隔离、事务故障注入、clock_changed、非法类型/枚举、非 UUID owner、跨 app 租约键与 Windows ctypes 签名/高 64 位句柄；`tests.test_migrations` 覆盖建库、幂等、初始化回滚、schema 篡改、约束、CLI 路径与固定 JSON。`tests.test_profile_facts` 覆盖事实库/审计/旧库拒绝/绑定；`tests.test_device_gate` 覆盖临时 OS 根下固定路径、跨进程互斥、完整 owner/profile 篡改、未知版本/额外对象不改写、崩溃/TTL/库不可读隔离、只读占用查询与写保护映射、活动检查；`tests.test_profile_session_lease` 覆盖合法 profile 库经 SqliteStore 领取/续租/失效、decide 桥前后拒绝与任务 owner 篡改；`tests.test_m4c_closed_loop` 覆盖真实活动恢复、目录路径不发布、实际 CLI eval 闭环与三值一致。Windows 停止检查器只用 mock ctypes，不读取真实 PID；测试不调用 ADB，也不写真实 `LOCALAPPDATA` 门禁。

## 五、维护要求

1. 修改 DDL、索引、租约/恢复规则或错误码时，同步更新本文件、`app/ports/AGENTS.md`、`app/domain/AGENTS.md`、根 `AGENTS.md`、`docs/operator.md` 与测试。
2. 不预建后续动作表、任务表、第二应用或迁移框架；v1 不做 1→2 升级；profile 库只追加当前事实/元数据/审计表，不伪造迁移或改五表合同。
3. 每个函数/方法写 docstring；新增故障注入点只用于测试，不接入生产 CLI。
