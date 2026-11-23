# app/domain 目录指南

## 一、目录用途

承载 M1 起的纯领域模型与无 IO 校验：任务状态、读取三态、未读快照、候选、动作证据和租约，以及 M2 的未读聚合与停止原因判定。该目录不导入 `sqlite3`、`adapters/`、`adb` 或具体应用控件，只依赖标准库与自身。

## 二、当前文件

- `__init__.py`：声明领域包。
- `models.py`：`UnreadSignal/UnreadRow/Candidate/UnreadSnapshot/ReadResult/Evidence/Owner/Lease` 数据类；§5.1 的 `canonical_hash`；会话身份、会话键重建/校验、发现键与指纹算法；毫秒 UTC 解析/格式化；任务/读取/信号/证据词表；`validate_read_result`、`validate_evidence`、`validate_lease` 等严格校验，拒绝 bool 冒充整数、非法枚举、坏 UUID/摘要与跨 app 值。
- `json_input.py`：CLI 与 BOSS 适配器共用的严格 JSON 解析；`load_json_object(text)` 固定使用 `reject_duplicate_keys`，在顶层/嵌套对象遇到重复键时抛 `ValueError`，不接受可配置 hook，也不负责文件 IO 或调用方错误分类。
- `scheduler.py`：`aggregate_unread(snapshot, unresolved_keys)` 只重算 `unread_state`；`stop_reason_for(snapshot, queued_keys)` 按 M2 有序判据返回 `null/unread_unverified/needs_targeted_verification`。两者均无 IO，非法参数抛 `ValueError`。

## 三、行为契约

1. `ReadResult` 为 `ok/failed/undetermined` 三态；ok 必须带完整 `UnreadSnapshot` 且无原因，failed/undetermined 必须 `value=null` 且原因属于各自固定词表。
2. `snapshot_id` 为规范 UUID 或 null，`observed_at` 为 `YYYY-MM-DDTHH:mm:ss.sssZ`。读取来源只取 `hierarchy/pixel/vision/human/fixture`，置信度只取 `low/medium/high`。
3. 未读四源固定为 `row_badge/new_greeting/tab_badge/preview_delta`；present/absent 无原因，unknown 与 not_applicable 的原因及计数组合按设计说明 §5.3 校验。
4. 会话键 `app:conv:H([联系人,公司,职位])`、指纹 `H([preview_hash,time_hint,unread_count])`、发现键 `app:discovery:H([conversation_key,fingerprint])`；H 使用键排序、`ensure_ascii=false`、紧凑分隔符和 SHA-256 小写十六进制。会话键必须精确等于 `app + ':conv:' + identity_hash`，发现键必须精确由会话键与指纹重算；伪造/替换任一侧都 `invalid_input`，不能靠“两个对象相等”证明身份一致。
5. `Evidence` 的 performed 必须有不同且非空的 before/after 快照、effect 非 none 且无原因；not_performed/unknown 必须 effect=none 且原因在各自词表。
6. `Owner.id` 为规范 UUID（本次进程启动 UUID），`Owner.host` 为 64 位小写十六进制本机摘要，`Owner.pid` 为正整数且不超过 SQLite 有符号 64 位上限；`Lease.key` 必须是当前 app 的 discovery 键。枚举/集合入口先做类型检查，非法类型仍抛 `ValueError` 供仓储映射 `invalid_input`。
7. 本目录不保存正文、不解密身份、不连接设备；M1 只用合成对象。M2 的 XML 解析在 `adapters/boss/list.py`，聚合与停止原因在 `scheduler.py`。
8. `aggregate_unread` 仅在任一适用源 present 时 present；只有 coverage=all、至少一个适用源、所有适用源 absent、无身份不可定位行且无未关闭发现时才 absent；其余 unknown。`not_applicable` 不参与“全部 absent”，全不适用仍为 unknown。
9. `stop_reason_for` 先看可领取 unread 候选（`null`）；任一前三源 present 或存在 unread 候选但不可领取为 `needs_targeted_verification`；再对前三源/聚合 unknown 返回 `unread_unverified`；仅 preview 正且其它源 absent/not_applicable 且有可领取 `new_message` 时返回 `null`；聚合 absent 返回 `null`；其余 `needs_targeted_verification`。

## 四、测试

```powershell
python -m unittest tests.test_store tests.test_migrations tests.test_sensor
```

领域校验通过仓储入口间接覆盖，包括 app 固定 boss、bool 计数、坏摘要/UUID/时间、候选键重算与证据组合；M2 聚合与停止原因由 `tests.test_sensor` 直接断言。

## 五、维护要求

1. 修改类型、词表、键/摘要算法、聚合或停止原因规则时，同步更新本文件、`app/ports/AGENTS.md`、`app/adapters/sqlite/AGENTS.md`、`app/adapters/boss/AGENTS.md`、根 `AGENTS.md` 和受影响测试。
2. 新模型必须有当前里程碑的真实消费者；不预建 M3+ 解析或动作状态。
3. 每个函数/方法写 docstring，文件顶部说明用途；不引入第三方依赖。

## 六、已知限制

- M2 只实现合成快照聚合；定向核对、像素/视觉读取与真实设备采集在 M3。
- `unread_state=present` 不证明候选可点击或计数完整；`unknown` 不得当成无未读。
