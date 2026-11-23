# app/domain 目录指南

## 一、目录用途

承载 M1 起的纯领域模型与无 IO 校验：任务状态、读取三态、未读快照、候选、动作证据和租约。该目录不导入 `sqlite3`、`adapters/`、`adb` 或具体应用控件，只依赖标准库与自身。

## 二、当前文件

- `__init__.py`：声明领域包。
- `models.py`：`UnreadSignal/UnreadRow/Candidate/UnreadSnapshot/ReadResult/Evidence/Owner/Lease` 数据类；§5.1 的 `canonical_hash`；会话身份、发现键与指纹算法；毫秒 UTC 解析/格式化；任务/读取/信号/证据词表；`validate_read_result`、`validate_evidence`、`validate_lease` 等严格校验，拒绝 bool 冒充整数、非法枚举、坏 UUID/摘要与跨 app 值。

## 三、行为契约

1. `ReadResult` 为 `ok/failed/undetermined` 三态；ok 必须带完整 `UnreadSnapshot` 且无原因，failed/undetermined 必须 `value=null` 且原因属于各自固定词表。
2. `snapshot_id` 为规范 UUID 或 null，`observed_at` 为 `YYYY-MM-DDTHH:mm:ss.sssZ`。读取来源只取 `hierarchy/pixel/vision/human/fixture`，置信度只取 `low/medium/high`。
3. 未读四源固定为 `row_badge/new_greeting/tab_badge/preview_delta`；present/absent 无原因，unknown 与 not_applicable 的原因及计数组合按设计说明 §5.3 校验。
4. 会话键 `app:conv:H([联系人,公司,职位])`、指纹 `H([preview_hash,time_hint,unread_count])`、发现键 `app:discovery:H([conversation_key,fingerprint])`；H 使用键排序、`ensure_ascii=false`、紧凑分隔符和 SHA-256 小写十六进制。
5. `Evidence` 的 performed 必须有不同且非空的 before/after 快照、effect 非 none 且无原因；not_performed/unknown 必须 effect=none 且原因在各自词表。
6. 本目录不保存正文、不解密身份、不连接设备；M1 只用合成对象。M2 的 XML 解析与四源聚合不在本目录实现。

## 四、测试

```powershell
python -m unittest tests.test_store tests.test_migrations
```

领域校验通过仓储入口间接覆盖，包括 app 固定 boss、bool 计数、坏摘要/UUID/时间、候选键重算与证据组合。

## 五、维护要求

1. 修改类型、词表、键/摘要算法或校验规则时，同步更新本文件、`app/ports/AGENTS.md`、`app/adapters/sqlite/AGENTS.md`、根 `AGENTS.md` 和受影响测试。
2. 新模型必须有当前里程碑的真实消费者；不预建 M2+ 解析或动作状态。
3. 每个函数/方法写 docstring，文件顶部说明用途；不引入第三方依赖。
