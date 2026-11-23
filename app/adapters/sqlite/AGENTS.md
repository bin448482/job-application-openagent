# app/adapters/sqlite 目录指南

## 一、目录用途

实现 `app/ports/store.py` 声明的 M1 本机 SQLite 仓储：空库 v1 建库/核验、候选入库、全局 primary 设备租约、任务 start/renew/finish/recover 与只读未关闭会话查询。不连接设备、不调用模型、不读取旧库。

## 二、当前文件

- `__init__.py`：声明 SQLite 适配器包。
- `schema.py`：五张 STRICT 表与五个索引的确定性 DDL；`initialize_database` 在单个 `BEGIN IMMEDIATE` 事务内建库或核验，任一步失败回滚；`connect_database` 打开现有 v1 库并设置 `foreign_keys=ON`、`busy_timeout=5000`；`verify_v1` 比较 `user_version`、对象集合与规范化 SQL，缺失、额外或篡改均报 `db_version_unsupported`。
- `repo.py`：`SqliteStore` 实现 upsert/claim/start/renew/finish/recover/unresolved_conversations；默认创建并可注入唯一 `ProcessLog`，打开库及公开操作以 `operation` 记录 started/ok/failed、固定原因码与耗时，不记 PID/host/nonce/键/正文/SQL/异常，业务事务与幂等不受影响；日志降级时由 ProcessLog 唯一告警出口最多提示一次，构造失败无实例时也可观察。`open_store` 边界先记录 started，再在连接前校验本机 host 摘要；非法 host 记 `invalid_input`/`store` 失败且不创建或改动数据库。默认本机 host 为 `H(platform.node())`，owner.id 必须是规范 UUID、owner.host 必须是 64 位小写十六进制本机摘要，租约 key 必须是当前 app 的 discovery 键。默认 Windows 停止检查器用 ctypes `OpenProcess`（同时请求查询权限与 `SYNCHRONIZE`）/`WaitForSingleObject`，显式声明 x64 HANDLE/DWORD/BOOL 签名；非 Windows 用 `os.kill(pid, 0)`。支持注入单调时钟、停止检查器与仅测试使用的故障点回调。

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
python -m unittest tests.test_store tests.test_migrations
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

`tests.test_store` 覆盖竞争、TTL、停止未知/异主机、旧 nonce/截止时间、重复 start/finish、blocked 设备隔离、事务故障注入、clock_changed、非法类型/枚举、非 UUID owner、跨 app 租约键与 Windows ctypes 签名/高 64 位句柄；`tests.test_migrations` 覆盖建库、幂等、初始化回滚、schema 篡改、约束、CLI 路径与固定 JSON。Windows 停止检查器只用 mock ctypes，不读取真实 PID；测试不调用 ADB。

## 五、维护要求

1. 修改 DDL、索引、租约/恢复规则或错误码时，同步更新本文件、`app/ports/AGENTS.md`、`app/domain/AGENTS.md`、根 `AGENTS.md`、`docs/operator.md` 与测试。
2. 不预建后续动作表、任务表、第二应用或迁移框架；v1 不做 1→2 升级。
3. 每个函数/方法写 docstring；新增故障注入点只用于测试，不接入生产 CLI。
