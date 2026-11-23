# app/domain 目录指南

## 一、目录用途

承载 M1 起的纯领域模型与无 IO 校验：任务状态、读取三态、未读快照、候选、动作证据和租约，以及 M2 的未读聚合/停止原因和 M3 的读取值类型、字段阶梯与稳定消息身份。该目录不导入 `sqlite3`、`adapters/`、`adb` 或具体应用控件，只依赖标准库与自身。

## 二、当前文件

- `__init__.py`：声明领域包。
- `models.py`：`UnreadSignal/UnreadRow/Candidate/UnreadSnapshot/ReadResult/Evidence/Owner/Lease` 数据类；§5.1 的 `canonical_hash`；会话身份、会话键重建/校验、发现键与指纹算法；毫秒 UTC 解析/格式化；任务/读取/信号/证据词表；`validate_read_result`、`validate_evidence`、`validate_lease` 等严格校验，拒绝 bool 冒充整数、非法枚举、坏 UUID/摘要与跨 app 值。
- `json_input.py`：CLI 与 BOSS 适配器共用的严格 JSON 解析；`load_json_object(text)` 固定使用 `reject_duplicate_keys`，在顶层/嵌套对象遇到重复键时抛 `ValueError`，不接受可配置 hook，也不负责文件 IO 或调用方错误分类。
- `reading.py`：M3 读取值类型 `FieldEvidence/PageSnapshot/JobCard/JobList/JobDetail/UnreadRowRead/UnreadRead/ChatMessage/Conversation`、稳定职位键/消息键算法、跨快照 `new_message_keys` 与各值类型严格校验；`validate_read_value` 是 `ReadResult.value` 的唯一分派入口。稳定消息身份只来自 profile 验证过的应用 `content-desc` 原值，内容摘要不生成消息键；不保存正文到日志/库。
- `ladder.py`：`FieldReading/FieldResolution` 与纯 `resolve_field`；层级→像素→视觉→人工顺序，前级 failed 不被后级成功掩盖，跨级冲突/上下文不一致转 undetermined/人工，视觉默认关闭且无独立交叉证据不放行。
- `scheduler.py`：`aggregate_unread(snapshot, unresolved_keys)` 只重算 `unread_state`；`stop_reason_for(snapshot, queued_keys)` 按 M2 有序判据返回 `null/unread_unverified/needs_targeted_verification`。两者均无 IO，非法参数抛 `ValueError`。

## 三、行为契约

1. `ReadResult` 为 `ok/failed/undetermined` 三态；ok 必须带完整 `UnreadSnapshot` 或 M3 读取值且无原因，failed/undetermined 必须 `value=null` 且原因属于各自固定词表。`validate_read_value` 只接受登记类型，未知类型/未知字段继续拒绝。
2. `snapshot_id` 为规范 UUID 或 null，`observed_at` 为 `YYYY-MM-DDTHH:mm:ss.sssZ`。读取来源只取 `hierarchy/pixel/vision/human/fixture`，置信度只取 `low/medium/high`。
3. 未读四源固定为 `row_badge/new_greeting/tab_badge/preview_delta`；present/absent 无原因，unknown 与 not_applicable 的原因及计数组合按设计说明 §5.3 校验。
4. M3 `PageSnapshot/JobCard/JobList/JobDetail/UnreadRead/Conversation` 全部键必填、拒绝额外键；页面/职位/未读/会话派生键必须由原值与固定 H 重算。`JobDetail.description` 原文只在内存/合成 CLI 数据区，摘要为 `H(description)`、长度按 Unicode 字符数。`UnreadRow` 状态/来源/计数/原因必须联动，`field_evidence` 从行结果重算。
5. 会话 `ChatMessage.content_hash` 只用于内容比较；`stable_identity_hash` 只来自 profile 显式验证的应用消息 `content-desc` 原值并复用固定命名域，`repeat_safe` 严格等于稳定身份是否存在。`inbound_state=known` 需读全、会话 contact/重算键核对、无 unknown 行、最新非系统消息 incoming 文本、未截断且稳定消息键唯一；否则 `none/non_text/unknown` 且无 `latest_inbound_key`。
6. `FieldReading/FieldResolution` 必须共享 app/profile_id/profile_version/snapshot_id/observed_at/layout_context；单级结论状态/计数/原因联动由校验器检查。`resolve_field` 只按层级→像素→视觉→人工合并，前级 failed 保留原因，冲突/上下文不同转 undetermined，视觉不能独立放行。
7. 会话键 `app:conv:H([联系人,公司,职位])`、指纹 `H([preview_hash,time_hint,unread_count])`、发现键 `app:discovery:H([conversation_key,fingerprint])`；H 使用键排序、`ensure_ascii=false`、紧凑分隔符和 SHA-256 小写十六进制。会话键必须精确等于 `app + ':conv:' + identity_hash`，发现键必须精确由会话键与指纹重算；伪造/替换任一侧都 `invalid_input`，不能靠“两个对象相等”证明身份一致。
8. `Evidence` 的 performed 必须有不同且非空的 before/after 快照、effect 非 none 且无原因；not_performed/unknown 必须 effect=none 且原因在各自词表。
9. `Owner.id` 为规范 UUID（本次进程启动 UUID），`Owner.host` 为 64 位小写十六进制本机摘要，`Owner.pid` 为正整数且不超过 SQLite 有符号 64 位上限；`Lease.key` 必须是当前 app 的 discovery 键。枚举/集合入口先做类型检查，非法类型仍抛 `ValueError` 供仓储映射 `invalid_input`。
10. 本目录不保存正文、不解密身份、不连接设备；M1/M2/M3 只用合成对象。M2 XML 解析在 `adapters/boss/list.py`，M3 页面/职位/会话解析在 `adapters/boss/read.py`，聚合与停止原因在 `scheduler.py`。
11. `aggregate_unread` 仅在任一适用源 present 时 present；只有 coverage=all、至少一个适用源、所有适用源 absent、无身份不可定位行且无未关闭发现时才 absent；其余 unknown。`not_applicable` 不参与“全部 absent”，全不适用仍为 unknown。
12. `stop_reason_for` 先看可领取 unread 候选（`null`）；任一前三源 present 或存在 unread 候选但不可领取为 `needs_targeted_verification`；再对前三源/聚合 unknown 返回 `unread_unverified`；仅 preview 正且其它源 absent/not_applicable 且有可领取 `new_message` 时返回 `null`；聚合 absent 返回 `null`；其余 `needs_targeted_verification`。

## 四、测试

```powershell
python -m unittest tests.test_store tests.test_migrations tests.test_sensor tests.test_read
```

领域校验通过仓储入口间接覆盖，包括 app 固定 boss、bool 计数、坏摘要/UUID/时间、候选键重算与证据组合；M2 聚合/停止原因由 `tests.test_sensor` 断言，M3 值类型、稳定消息/职位键、阶梯与跨屏身份由 `tests.test_read` 断言。

## 五、维护要求

1. 修改类型、词表、键/摘要算法、聚合或停止原因规则时，同步更新本文件、`app/ports/AGENTS.md`、`app/adapters/sqlite/AGENTS.md`、`app/adapters/boss/AGENTS.md`、根 `AGENTS.md` 和受影响测试。
2. 新模型必须有当前里程碑的真实消费者；不预建 M4+ 解析或动作状态。
3. 每个函数/方法写 docstring，文件顶部说明用途；不引入第三方依赖。

## 六、已知限制

- M2 只实现合成快照聚合；M3 只实现合成页面/职位/未读/会话读取、像素字段后备与跨屏稳定身份；真实设备采集、导航、视觉与正文持久化未实现。
- `unread_state=present` 不证明候选可点击或计数完整；`unknown` 不得当成无未读。M3 `actionable=true` 只是离线门禁，不提供发送授权。
