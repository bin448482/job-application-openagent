# app/domain 目录指南

## 一、目录用途

承载 M1 起的纯领域模型与无 IO 校验：任务状态、读取三态、未读快照、候选、动作证据和租约，M2 的未读聚合/停止原因，M3 的读取值类型、字段阶梯与稳定消息身份，以及 M4a 的 DecisionEnvelope/Decision 契约、策略包校验、摘要与受限渲染。该目录不导入 `sqlite3`、`adapters/`、`adb` 或具体应用控件，只依赖标准库与自身。

## 二、当前文件

- `__init__.py`：声明领域包。
- `models.py`：`UnreadSignal/UnreadRow/Candidate/UnreadSnapshot/ReadResult/Evidence/Owner/Lease` 数据类；§5.1 的 `canonical_hash`；会话身份、会话键重建/校验、发现键与指纹算法；毫秒 UTC 解析/格式化；任务/读取/信号/证据词表；`validate_read_result`、`validate_evidence`、`validate_lease` 等严格校验，拒绝 bool 冒充整数、非法枚举、坏 UUID/摘要与跨 app 值。
- `json_input.py`：CLI 与 BOSS 适配器共用的严格 JSON 解析；`load_json_object(text)` 固定使用 `reject_duplicate_keys`，在顶层/嵌套对象遇到重复键时抛 `ValueError`，不接受可配置 hook，也不负责文件 IO 或调用方错误分类。
- `reading.py`：M3 读取值类型 `FieldEvidence/PageSnapshot/JobCard/JobList/JobDetail/UnreadRowRead/UnreadRead/ChatMessage/Conversation`、稳定职位键/消息键算法、跨快照 `new_message_keys` 与各值类型严格校验；`validate_read_value` 是 `ReadResult.value` 的唯一分派入口。稳定消息身份只来自 profile 验证过的应用 `content-desc` 原值，内容摘要不生成消息键；不保存正文到日志/库。
- `ladder.py`：`FieldReading/FieldResolution` 与纯 `resolve_field`；层级→像素→视觉→人工顺序，reading 的 source 必须与调用槽位一致，前级 failed 在上下文比较前保留，跨级冲突/上下文不一致转 undetermined/人工且只携带主上下文证据，视觉默认关闭且无独立交叉证据不放行；枚举/原因/计数/flag 先做类型检查，坏类型统一 ValueError。
- `scheduler.py`：`aggregate_unread(snapshot, unresolved_keys)` 只重算 `unread_state`；`stop_reason_for(snapshot, queued_keys)` 按 M2 有序判据返回 `null/unread_unverified/needs_targeted_verification`。两者均无 IO，非法参数抛 `ValueError`。
- `policy.py`：M4a `DecisionEnvelope`/`Decision` 严格校验、`parse_envelope`/`parse_decision`、租约参数位置 `validate_decision(..., *, now, lease_expires_at)`；TOML+三 md schema、45 组合唯一覆盖与 A/B/C 硬下限、`effective_limits`、`resolve_policy_tier(pack, intent, sub_intents, escalate, upper_tier)`；`compute_base_hash/compute_pack_hash/compute_prompt_hash/compute_input_hash` 与无自引用投影；固定 system/developer/user `render_request` 与唯一 `{base,pack,data}` 内容对象；`append_and_validate_reply` 追加披露并复验码点/控制字符/联系方式/禁谈项/事实值；`validate_policy_guides` 检查可选双指南编码/控制字符/联系方式；`v1_policy_tier()` 固定 C。只有标准库与自身依赖。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、行为契约

1. `ReadResult` 为 `ok/failed/undetermined` 三态；ok 必须带完整 `UnreadSnapshot` 或 M3 读取值且无原因，failed/undetermined 必须 `value=null` 且原因属于各自固定词表。`validate_read_value` 只接受登记类型，未知类型/未知字段继续拒绝。
2. `snapshot_id` 为规范 UUID 或 null，`observed_at` 为 `YYYY-MM-DDTHH:mm:ss.sssZ`。读取来源只取 `hierarchy/pixel/vision/human/fixture`，置信度只取 `low/medium/high`。
3. 未读四源固定为 `row_badge/new_greeting/tab_badge/preview_delta`；present/absent 无原因，unknown 与 not_applicable 的原因及计数组合按设计说明 §5.3 校验。
4. M3 `PageSnapshot/JobCard/JobList/JobDetail/UnreadRead/Conversation` 全部键必填、拒绝额外键；页面/职位/未读/会话派生键必须由原值与固定 H 重算。`JobDetail.description` 原文只在内存/合成 CLI 数据区，摘要为 `H(description)`、长度按 Unicode 字符数。`UnreadRow` 状态/来源/计数/原因必须联动，`field_evidence` 从行结果重算。
5. 会话 `ChatMessage.content_hash` 只用于内容比较；`stable_identity_hash` 只来自 profile 显式验证的应用消息 `content-desc` 原值并复用固定命名域，`repeat_safe` 严格等于稳定身份是否存在。`inbound_state=known` 需读全、会话 contact/重算键核对、无 unknown 行、最新非系统消息 incoming 文本、未截断且稳定消息键唯一；重复稳定身份、截断或未知行会使整个会话入站未知。`new_message_keys` 只比较同 app/profile/conversation 的合法快照，跨会话直接 ValueError；跨屏合并必须同时遵守所有参与页的 `message_identity_verified`。
6. `FieldReading/FieldResolution` 必须共享 app/profile_id/profile_version/snapshot_id/observed_at/layout_context；单级结论状态/计数/原因联动由校验器检查。`resolve_field` 只按层级→像素→视觉→人工合并，source 必须匹配调用槽位，前级 failed 保留原因，冲突/上下文不同转 undetermined，视觉不能独立放行；像素/人工来源与置信度不得冒充 hierarchy/high，结果校验拒绝与证据不符的派生状态/计数，并对已决结果用同一 `resolve_field` 规则重算核对。
7. 会话键 `app:conv:H([联系人,公司,职位])`、指纹 `H([preview_hash,time_hint,unread_count])`、发现键 `app:discovery:H([conversation_key,fingerprint])`；H 使用键排序、`ensure_ascii=false`、紧凑分隔符和 SHA-256 小写十六进制。会话键必须精确等于 `app + ':conv:' + identity_hash`，发现键必须精确由会话键与指纹重算；伪造/替换任一侧都 `invalid_input`，不能靠“两个对象相等”证明身份一致。
8. `Evidence` 的 performed 必须有不同且非空的 before/after 快照、effect 非 none 且无原因；not_performed/unknown 必须 effect=none 且原因在各自词表。
9. `Owner.id` 为规范 UUID（本次进程启动 UUID），`Owner.host` 为 64 位小写十六进制本机摘要，`Owner.pid` 为正整数且不超过 SQLite 有符号 64 位上限；`Lease.key` 必须是当前 app 的 discovery 键。枚举/集合入口先做类型检查，非法类型仍抛 `ValueError` 供仓储映射 `invalid_input`。
10. 本目录不保存正文、不解密身份、不连接设备；M1/M2/M3 只用合成对象。M2 XML 解析在 `adapters/boss/list.py`，M3 页面/职位/会话解析在 `adapters/boss/read.py`，聚合与停止原因在 `scheduler.py`。
11. `aggregate_unread` 仅在任一适用源 present 时 present；只有 coverage=all、至少一个适用源、所有适用源 absent、无身份不可定位行且无未关闭发现时才 absent；其余 unknown。`not_applicable` 不参与“全部 absent”，全不适用仍为 unknown。
12. `stop_reason_for` 先看可领取 unread 候选（`null`）；任一前三源 present 或存在 unread 候选但不可领取为 `needs_targeted_verification`；再对前三源/聚合 unknown 返回 `unread_unverified`；仅 preview 正且其它源 absent/not_applicable 且有可领取 `new_message` 时返回 `null`；聚合 absent 返回 `null`；其余 `needs_targeted_verification`。
13. M4a Envelope 全部键必填、拒绝额外键；`schema_version=1`、规范 UUID nonce、64hex 摘要、`app` 注册表、kind 词表、kind 最小 subject、非空 conversation_key 必须是 `app:conv:<64hex>`、非空 inbound、最多两条 context、事实固定四键且 key 在注册表、`source=user_confirmed`、未过期（`expires_at<=now` 拒绝）、重复键拒绝；constraints 为 `max_chars 1–300`、`no_contact=true`、`max_bytes=2048`；完整 envelope 规范 UTF-8 ≤2048 字节，超限 `input_too_large`。`input_hash` 用完整 envelope 去自身重算。subject 的次要键为 string|null 并允许合法空字符串，按 kind 只要求必需字段非空；Envelope 与 preview 共用 `validate_subject_fields`。
14. Decision 全部键必填、拒绝额外键；前三个标识逐字回显，不一致 `stale_decision`；intent/子意图词表、去重；reply 0–300 码点、只放行 LF、拒绝其它控制字符与孤立代理；evidence 必须为 inbound 非空子串；`needs_human/uncertain` 必须 `human_review`。租约有效期不在 Envelope 内，由调用者传入可信 `lease_expires_at`（M1 `Lease.expires_at`），`now>=lease_expires_at` 拒绝。
15. H 只使用键排序、UTF-8、紧凑分隔符的 SHA-256，不做 strip/换行/Unicode 规范化。`base_hash=H({项目相对 base 路径:原文})`；`pack_hash=H({schema_version:1,files:{四文件名:原文}})`；`prompt_hash=H({base:{version,hash,files},pack:{hash,config,files},data:envelope 去 task_id/nonce/input_hash/prompt_hash})`；`input_hash` 最后计算。唯一规范 `RenderedRequest.content` 恰为 `{base,pack,data}` 且 `H(content)=prompt_hash`；协议/preview 消息只作外层包装。摘要不得包含自身；目录名/时间不入 pack 摘要。
16. 策略包 TOML 与三 md 全部键/语法必填；45 个 `intent.sub_intent` 恰好覆盖一次，通配只支持 `intent.*`，同档重复/跨档冲突/漏项拒绝。硬下限由唯一 `HARD_FLOOR_TIERS` 派生：仅 interested.greeting/job_question 可 A，interested.resume_request/interview/salary/jd_request 至少 B，其余 39 键固定 C；校验、`resolve_policy_tier` 与展示共用该源，拒绝越权但不全包强制 C。有效限制取 base min/max，禁止项取并集，事实键取注册表/包白名单交集输入。披露 1–80 码点、含 `AI助手`、单行、无联系方式，且不得命中 never-say。pack 文件大小/路径/链接/BOM/控制字符/联系方式/可确定个人事实模式在适配器与领域共同拒绝；有限模式不声称完整语义判断。
17. `render_request` 只选当前 kind 的一份 base 作为 system，有效策略为 developer，envelope 投影为 user JSON；渲染前从四载荷原文重解析派生结构并复核摘要，避免同摘要不同实际策略内容；完整 `{base,pack,data}` 内容对象 ≤64 KiB，同时保留实际三分区消息上限；预览不调用桥/模型/设备，`v1_policy_tier()` 固定 C。
18. 构建器 `select_allowed_facts` 以显式 now 取包白名单/注册表/`user_confirmed`/未过期交集；同一键重复或值/有效期冲突时整键受控丢弃、留给待办，不做同值去重；不猜测、不编造来源。公开 validator 仍严格拒绝到期/非法结构。回复复验只识别明确经验/薪资/地点形式：经验须有年/个月/月单位，普通“我有3个问题”不识别；数值保留小数点、范围端点与单位，允许的薪金单位为 k/千、万/万元、元或无单位，其它后缀/数量级/未知单位/范围与单值不一致均 conflict；地点按全文严格相等，不做双向子串；未识别语义不因此放行。

## 四、测试

```powershell
python -m unittest tests.test_store tests.test_migrations tests.test_sensor tests.test_read tests.test_policy_contract tests.test_policy_pack
```

领域校验通过仓储入口间接覆盖，包括 app 固定 boss、bool 计数、坏摘要/UUID/时间、候选键重算与证据组合；M2 聚合/停止原因由 `tests.test_sensor` 断言，M3 值类型、稳定消息/职位键、阶梯与跨屏身份由 `tests.test_read` 断言；M4a 契约、摘要、45 组合、渲染与披露复验由 `tests.test_policy_contract`、`tests.test_policy_pack` 断言。

## 五、维护要求

1. 修改类型、词表、键/摘要算法、聚合、停止原因或 M4a 契约时，同步更新本文件、`app/ports/AGENTS.md`、`app/adapters/sqlite/AGENTS.md`、`app/adapters/boss/AGENTS.md`、`app/adapters/files/AGENTS.md`、根 `AGENTS.md` 和受影响测试。
2. 新模型必须有当前里程碑的真实消费者；不预建 M4b 桥、动作或运行时调度模型。
3. 每个函数/方法写 docstring，文件顶部说明用途；不引入第三方依赖。

## 六、已知限制

- M2 只实现合成快照聚合；M3 只实现合成页面/职位/未读/会话读取、像素字段后备与跨屏稳定身份；M4a 只实现纯契约、策略包/摘要/渲染与回复复验，真实设备采集、导航、视觉、正文持久化、模型桥与真实 A 档判据未实现。
- `unread_state=present` 不证明候选可点击或计数完整；`unknown` 不得当成无未读。M3 `actionable=true` 只是离线门禁，不提供发送授权。M4a 的 pattern/长度/禁谈检查只是有限确定性门禁，未命中不等于语义安全，最终档位固定 C。
