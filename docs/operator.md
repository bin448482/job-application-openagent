# M0 只读探针、M1 状态库、M2 合成传感器、M3 离线读取与 M4a 策略包操作手册

> 文件用途：说明当前已实现的 M0 `doctor`、M1 `store init`、M2 `sense`、M3 `read` 与 M4a `policy` 能做什么、怎么运行、如何解读 JSON，以及在执行真实设备探针/采集前必须完成的确认与边界；M4b 真实桥/eval、M4c 真实活动检查及 M5 以后的能力尚未实现。

## 一、当前可用范围

已实现五组离线入口：

- `doctor`：一次独立的只读探针，读取 ADB 设备清单或合成 fixture，并输出设备数量、所选逻辑别名、解锁状态和前台应用类别。
- `store init`：创建或核验设计说明 §6 的本机 SQLite v1 状态库（五表、约束、索引与用户版本 1），不导入旧库。
- `sense`：读取合成 sensor manifest/profile/XML，按 M2 四源规则解析、合并、落库候选并输出有序停止原因。
- `read`：读取 M3 合成页面/职位/未读/会话 manifest/profile/XML 与可选合成像素标定，执行层级/像素/人工读取阶梯和跨屏合并，输出 `ReadResult` 与决策门禁；不连接业务库。
- `policy`：M4a 策略包六命令，离线完成初始化、TOML/Markdown 校验、纯渲染预览、活动包/快照列表、快照与恢复；不连接设备、业务库、模型或网络。

M0–M4a 都不切换真实应用、不 dump 真实聊天、不截图、不点击、不发送、不调用网络或模型。`doctor --fixture` 只读取合成 JSON，永远不启动子进程；`store init` 只写项目内本机 SQLite；`sense`/`read` 只读合成输入，`sense` 可写项目内合成 SQLite，`read` 不写业务库；`policy` 只读写项目内公开 base、示例、固定 preview 案例与本机 `prompts/local`。每次运行创建自己的被忽略 `data/logs/m0-process-<运行 UUID>.jsonl` 本机过程日志（见第十一节），运行之间不共享追加。

M1 的仓储 API 已实现候选入库、任务租约、三态收敛与过期恢复；M2 `sense` 只调用 `upsert_snapshot` 与 `unresolved_conversations`，**不** claim/start/finish、不消耗发现、不推进已读基线、不打开会话。M3 `read` 不调用仓储、不关闭发现、不写正文或 `verified` 基线；M4a `policy` 不调用桥、仓储或设备。M4b 真实桥/eval、M4c 真实活动检查、M5+ 的决策运行、动作与调度仍未实现。

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

## 四、M1 状态库初始化（`store init`）

首次创建或重复核验：

```powershell
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

成功时退出 `0`，只向 stdout 输出固定对象：

```json
{"schema_version":1,"status":"ok","reason_code":null}
```

规则：

- `--db` 省略时为 `data/runtime/runtime.sqlite3`；
- 路径只接受项目内相对路径；解析符号链接和 `..` 后仍须位于项目根内，否则 `invalid_input`；
- 空库首次初始化在一个写事务内创建五张 STRICT 表、全部约束/外键/索引并设置 `user_version=1`；中途失败整体回滚，不残留半套表；
- 已存在的完整 v1 库只核验结构，成功幂等且不改变任何业务行；未知版本、非空无版本库或结构被增删改（表、列、约束、索引）都拒绝，绝不覆盖或重建；另一连接持写锁且等待耗尽时报 `store_busy`，释放后重试可幂等成功；
- 不导入旧项目库，不读取根 `config.json`、环境文件、凭据或个人事实；
- 只写本机 SQLite，不连接设备、网络或模型。

v1 结构摘要（以设计说明 §6 为准）：

| 表 | 用途 |
|---|---|
| `hr_conversations` | 会话身份摘要与可选的真实读全基线；M1 不写基线。 |
| `hr_discoveries` | M1–M2 的候选队列：会话键、指纹、四源 JSON、快照 ID、`opened=0` 与公共任务状态/租约列。 |
| `hr_messages` | 为后续读全消息预留；M1 不写入。 |
| `device_lease` | 跨任务唯一的 `primary` 设备占用行；`held` 或 `quarantined`。 |
| `events` | 只追加白名单元数据事件，不含正文。 |

公共任务状态为 `queued/leased/running/done/not_sent/failed/blocked/cancelled`。任务与设备共享同一 owner/pid/host/nonce/截止时间；租约 TTL 为 60 秒。M1 只允许 `app=boss`，第二应用需后续版本迁移，不预建迁移框架。

仓储行为（通过测试或未来用例调用，不在此手工作业）：

- `upsert_snapshot` 只接受 `ReadResult.status=ok`、非空快照 ID 的完整合成结果；未知行不落库，同指纹重放不追加事件，queued 可升级 `new_message→unread`。
- `claim` 在任一 `device_lease` 行存在（含过期或 quarantined）时返回 `lease_busy`；仅 TTL 到期不会释放占用。owner.id 必须是规范 UUID，owner.host 必须是本机 `H(platform.node())` 摘要；Lease.key 必须是当前 app 的 discovery 键，非法类型/枚举、非 UUID owner 与跨 app 键统一 `invalid_input`。
- `start` 只允许 `leased→running` 且不延长截止时间；`renew` 同步延长任务与设备；旧 Lease、旧 nonce 或重复操作返回 `stale_lease`。
- `finish` 只允许 `running→done/not_sent/failed/blocked`，原因必须与读取/证据三态匹配。blocked 清空任务租约，但保留设备原 owner/pid/host/nonce/截止时间，仅把 `reservation_state` 置为 `quarantined`；下一次 claim 仍为 `lease_busy`。
- `recover` 仅在任务/设备身份与 nonce 匹配、已过期、`confirm_stopped=true`、本机 host 匹配且注入的停止检查器判定 `stopped` 后生效：leased 回 `queued` 并删除设备行；running 转 `blocked/recovery_requires_reconciliation` 并隔离设备。未过期返回 `lease_busy`，其余停止/主机不确定返回 `owner_not_stopped`。
- 墙钟与单调时钟增量相差超过 1 秒返回 `clock_changed`，不执行新动作。
- 所有写操作使用 `BEGIN IMMEDIATE`，任务、设备或事件任一行更新/插入失败都整体回滚。

`data/runtime/` 是自动生成的本机产物目录，被 Git 忽略；不要提交数据库、事件或日志。

## 五、M2 合成传感器（`sense`）

运行验收正例：

```powershell
python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3
```

退出 `0`，stdout 只有一个 JSON，外层固定为 `schema_version/result/stop_reason`；`result` 是完整 `ReadResult`（`status/value/reason_code/snapshot_id/observed_at/source/confidence`），`value` 是 `app/profile_id/coverage/signals/rows/candidates/unread_state`。正例中 `status=ok`、`value.unread_state=present`、`stop_reason=null`、唯一候选 `kind=unread`；连续执行第二次仍退出 `0`，`hr_discoveries` 总数保持 `1` 且不追加事件。

规则：

- 必须提供 `--fixture <项目内相对文件>`，不接受 `--device`/`--serial`；`--db` 省略为 `data/runtime/runtime.sqlite3`，只接受项目内相对路径。
- manifest 固定 `{profile_path,xml_paths,app_version,observed_at,baseline_rows,coverage_proof}`；profile 固定 M2 最小字段。所有键必填、拒绝额外键；路径相对 manifest 目录，且必须是同一夹具目录内现有文件，拒绝绝对路径、`..`、符号链接/junction 与越界。`badge_region` 元素先按类型与 0–1 范围校验，再对浮点判有限性；巨大整数不得溢出，非法区域为 `invalid_fixture`。
- 一个 manifest 的 `xml_paths` 去重且非空；`coverage_proof.kind=viewport` 时只能一页。多页共用同一个规范 UUID `snapshot_id` 与 `observed_at`。`baseline_rows` 的会话键必须带 `boss:conv:` 前缀，`preview_hash` 为 SHA-256 小写十六进制摘要或 `null`，`time_hint` 为字符串或 `null`；非法摘要、重复键或额外键均 `invalid_fixture`，不得把无比较基线当作未变化。
- XML 用标准库 ElementTree，只接受唯一 `hierarchy` 根，根内只允许 `node` 元素（嵌套 `hierarchy` 也拒绝）；`DOCTYPE`/`ENTITY`、损坏 XML、非法 Unicode/编码、其它元素为 `failed/invalid_xml`；夹具 XML 文件 UTF-8 解码失败也映射 `invalid_xml`。唯一列表缺失为 `failed/wrong_page`，多列表或嵌套行为 `undetermined/incomplete_snapshot`。
- 行身份只接受行内唯一节点的原样非空文本；`identity_hash=H([姓名,公司,职位])`，会话键为 `boss:conv:<identity_hash>`。身份缺失或重复不产生候选。
- 行角标只认完全落在 profile 比例区域内的 `android.widget.TextView` 唯一 ASCII 十进制整数；行字段（正文/时间）节点不参与角标识别。任一候选数字节点几何不可读时优先判为 `uncalibrated_region`，不能被同行的合法计数掩盖；超大行坐标导致的区域换算溢出也回到该未知语义。null 区域或非法 bounds 为 `uncalibrated_region`，多个合法整数节点为 `ambiguous_signal`，无节点为 `missing_signal`。
- 新招呼与底部 tab 只按标签精确文本与直接父容器唯一计数节点解析；正值 present，`0+` 为歧义；未验证语义的新招呼 0 与未验证的 tab 0 为 `unverified_semantics`。`not_applicable_sources` 只能由已验证合成 profile 声明，全部不适用仍为 unknown。
- 单页 coverage 恒为 `viewport`；只有完整遍历首尾证明均真或已标定全局零信号可声明 `all`。全局零只提供 coverage，不把未完整遍历的行角标或缺基线预览改为 absent。
- 分页只合并相邻页连续、整行字段完全一致的最长重叠段；其它重复身份保留 `identity_hash` 但会话键为 `null`。合并后重算四源信号、指纹与候选，不按页拼接。成功页必须共享同一规范 UUID `snapshot_id`；多个 `null` 成功页不是同次采集，返回 `failed/invalid_fixture`。M1 已失败/未决页面的 `snapshot_id=null` 契约不变。
- `observed_at` 距 `now` 不在 0–5 秒或早于 `now`（未来快照）为 `undetermined/stale_snapshot`；`calibrated=false` 或 `app_version` 不等于 `profile.version` 为 `undetermined/uncalibrated_profile`。失败/未决时 `result.value=null`、不写库。
- 只有输入全部有效且合并 `status=ok` 后才初始化/打开 `--db`；先 `upsert_snapshot`，再查询 `unresolved_conversations`，调用领域 `aggregate_unread`，最后按顺序得出 `stop_reason`。
- `stop_reason=null` 退出 `0`；`unread_unverified` 或 `needs_targeted_verification` 退出 `2`。读取本身失败/未决时 `stop_reason=result.reason_code` 并退出 `2`，不写库。仓储错误只输出三键安全对象，不伪造成功的 ReadResult。
- `sense` 不 claim/start/finish、不消耗发现、不打开会话、不推进已读基线；M3 `read` 提供离线定向核对接口与门禁，但真实导航/动作属于后续共享门禁与 M4+。

夹具布局为 `tests/fixtures/sensor/positive.json`（manifest）、`profile.json`、`positive.xml` 与仅供测试读取的 `expected.json`。解析器不读取 `expected.json`；其它正反例由测试在临时目录生成。合成 profile 不是已验证真实版本。

## 六、M3 离线读取（`read`）

运行验收正例：

```powershell
python -m app read --fixture tests/fixtures/read/page.json
python -m app read --fixture tests/fixtures/read/job_list.json
python -m app read --fixture tests/fixtures/read/job_detail.json
python -m app read --fixture tests/fixtures/read/unread.json
python -m app read --fixture tests/fixtures/read/conversation.json
```

输出固定外层：

```json
{"schema_version":1,"result":{...ReadResult...},"actionable":true,"gate_reason":null}
```

`status=ok` 且 `actionable=true` 退出 `0`；`failed/undetermined` 或结构 ok 但字段未知/身份未决时退出 `2`，`result.value=null` 仅用于失败/未决。`gate_reason` 固定为 `not_ok/field_unknown/identity_ambiguous/incomplete/no_target`。门禁只判断读取能否进入后续决策，不执行模型、动作或发送；合成 `actionable=true` 不提供真实发送授权。

规则：

- 必须提供 `--fixture <项目内相对文件>`；不接受 `--device`、`--serial`、`--db`，未知选项为 `invalid_input`。不存在真实 `read --device` 入口。
- manifest 固定 `{profile_path,app_version,observed_at,kind,hierarchy_paths,coverage_proof,conversation_context,pixel}`；`kind` 仅 `page/job_list/job_detail/unread/conversation`。所有键必填、拒绝额外键；引用相对 manifest 目录且必须在同一夹具目录内，拒绝绝对路径、`..`、链接/junction、越界与缺失文件。
- `page/job_list/job_detail/unread` 恰好一页；`conversation` 一页或多页。`pixel` 仅 `unread` 可为 `{calibration_path,raw_path}`；`conversation_context` 仅 `conversation` 必需，形状为 `{contact,company,position,source_snapshot_id}`。其它组合为 `invalid_fixture`。
- profile 固定 `{profile_id,app,version,calibrated,selectors}`；selectors 必填页面/职位/聊天/未读后缀、区域、标记与稳定标识声明。`calibrated=false` 或 `app_version` 不等于 profile.version 为 `undetermined/uncalibrated_profile`。
- `PageSnapshot`：按弹窗 → 登录 → 聊天详情 → 职位详情 → 推荐流 → 聊天列表 → 底部标签 ≥2 → unknown 分类。页面 unknown 仍可 `status=ok`，但不可决策。
- `JobList`：只提取当前视口完整职位卡；标题只去首尾 `@%&` 与空格，公司/薪资只去首尾空白；`job_key="boss:job:"+H([title,company,salary])`。重复身份保留摘要但不可定位；零张完整卡为 `undetermined/incomplete_snapshot`。
- `JobDetail`：必需正文与沟通按钮，缺失为 `failed/wrong_page`；正文含“查看更多”或以 `…`/`...` 结尾为 `undetermined/incomplete_snapshot`，不返回截断部分值。正文原文只在内存/合成 CLI 数据区，不写日志/事件/库；同时输出 `description_hash/length/complete`。缺招聘者/地点保留 null 并列入 `missing_fields`。
- `UnreadRead`：行身份仍用 `H([联系人,公司,职位])`；重复/缺失身份不生成会话键。角标只在标定行区域内读取唯一 TextView ASCII 整数；层级显式 `0` 为 absent/count=0。像素只作层级未知后的字段后备，显式合成标定包含 `calibration_id/app/profile_id/version/calibrated/absent_state_verified/resolution/badge_region/target_color/tolerance/threshold_pixels/source/calibrated_on`；本版本只接受 `source=synthetic`、真实日历日期、RGBA `format=1/colorspace=0`，像素 present 不生成精确计数。分辨率/profile/version 不一致为 `unknown/calibration_mismatch`；损坏头/长度/格式/越界为失败；低于阈值仅在 `absent_state_verified=true` 且几何匹配时为 absent，否则 unknown。
- `Conversation`：实际标题必须与上下文 contact 逐字一致；`conversation_key` 由实际 contact + 上下文 company/position 重算，不信任 manifest 自报键。该上下文只是合成发现/采集链假设，不是仓储来源认证；真实接线必须核对本轮已保存发现与同一次导航/采集链，证据不足为 `undetermined/identity_ambiguous`。
- 消息内容指纹 `content_hash` 只用于内容比较；可发送消息身份只能来自 profile 显式验证的应用消息稳定标识 `content-desc`，`stable_identity_hash=H(["boss","message","stable-id",raw])`，消息键 `boss:msg:H([conversation_key,stable_identity_hash])`。同正文、同秒、坐标、屏幕序号、布局 resource-id 或当次 UUID 都不构成稳定身份；未标定真实版本保持 `unknown`，不生成入站键。
- 跨屏只裁剪相邻页边界上稳定身份与 kind/direction/content_hash/time_hint 完全一致的连续最长重叠；完全相同的画面且无稳定身份不自动去重。重复稳定 ID、方向冲突、截断、读未全、存在 unknown 行或最新非系统消息为 outgoing 时不生成可发送入站身份。
- 读全要求单页列表显式 `scrollable="false"`，或 `coverage_proof.kind=full_traversal` 且首尾证明均真、所有页 `ok`；否则 `undetermined/incomplete_snapshot`。角标消失、打开过或单张截图都不证明无新入站或关闭发现。
- 字段阶梯：层级默认优先；仅字段 undetermined 才用像素；人工仅来自显式输入；视觉默认关闭，启用但无独立交叉证据也转人工。前级已确认字段不被后级覆盖；跨级冲突 `undetermined`/人工；前级 `failed` 不被后级 unknown/成功掩盖。参与同一字段的结果必须共享 app/profile_id/version/snapshot/observed_at/版式上下文。
- `read` 不调用 `upsert_snapshot`/`claim/start/finish`，不写 `hr_messages`、不关闭发现、不推进 `verified` 基线。正文持久化与跨屏消息表是后续显式迁移依赖，不在本轮修改 v1 五表。
- 真实采集适配器 `AdbScreenReader` 只用参数数组、`shell=False`、5 秒超时、不重试，并保存本机证据；测试只 mock subprocess。真实 read 依赖共享 OS 设备门禁，M4 门禁未交付前 usecase 在任何子进程启动前拒绝，不能用 profile 内 primary 任务锁替代。本仓库未执行真实 ADB。
- 详细字段/枚举/失败边界见 [M3 契约文档](BOSS直聘_M3读取阶梯与最小数据契约_20261124.md)。

## 七、M4a 策略包离线编辑、校验、预览与回滚

M4a 只交付策略包契约、公开 base/示例、确定性渲染和本机文件事务，不代表完整 M4、M4b 模型桥/eval、M4c 运行绑定或真实发送授权。所有命令从项目根执行，只接受项目内相对路径，显式 `--pack`/`--dest`/`--snapshot`，不自动读取根 `config.json`。

### 7.1 初始化

```powershell
python -m app policy init --dest prompts/local/my-profile
```

- 只允许 `prompts/local/<profile>` 直接子目录；目录名必须符合 profile_id 词法，目标存在报 `policy_exists`，不覆盖。
- 从 `prompts/examples/default/` 复制四载荷文件，只把唯一 `profile_id` 行改为目标目录名，并生成固定 `AGENTS.md`/`CLAUDE.md` 双指南。
- 先在点前缀暂存目录写入并校验，再同卷改名发布；失败清理本次自有路径，不留可加载半包。

### 7.2 编辑四个载荷

非程序使用者只编辑以下文件：

- `tone.md`：通用语气，普通非空文本。
- `phrase-bank.md`：`## intent.sub_intent` 标题与 `- 通用短句`；可空文件。
- `never-say.md`：`- 禁用字面表达`；可空文件，程序逐字匹配最终完整回复。
- `pack.toml`：披露、字数/冷却/每日上限、A/B/C 分档、额外禁谈主题、三份 md 文件名、事实白名单。公开示例为全 C、空事实；只能按策略包说明 §3.2 的硬下限调整。硬下限由同一数据源派生：仅 `interested.greeting`/`interested.job_question` 允许 A，`interested.resume_request`/`interview`/`salary`/`jd_request` 至少 B，其余 39 个组合固定 C；把固定 C 组合放入 B/A 会在具体数组项报 `safety_floor`，但不会把合法 A/B 编辑全包强制 C。A 档不授予发送权限。

不要写个人事实、真实聊天、联系方式、凭据或旧个人策略。四载荷及可选双指南须为普通文件；拒绝链接/junction、BOM、NUL、非 UTF-8、`pack.toml` >16 KiB、单份 md >32 KiB、四文件合计 >64 KiB、联系方式模式、可确定识别的个人事实形式与非法 Markdown 语法。

### 7.3 校验与预览

```powershell
python -m app policy validate --pack prompts/local/my-profile
python -m app policy preview --pack prompts/local/my-profile --case policy-greeting
```

`validate` 读取两份公开 base 与活动包，输出 profile_id、base_version、base_hash、pack_hash、有效限制、展开后的 A/B/C、硬下限、来源相对文件/字段/行与文件列表。成功退出 `0`；坏包退出 `2`，`errors` 定位相对文件、字段与 1 起行号，未知键只定位父表，不回显原值。

`preview` 只读固定 `tests/fixtures/policy-preview/` 的独立合成案例（发布清单固定包含 `policy-greeting`，另有 job/anomaly/injection），用当前 base/包实际构建 envelope 与 `system/developer/user` 三分区投影，输出完整渲染、有效限制、硬下限、来源与四类摘要。它不读取 `evals/cases.json`、参考 provider、expected、私有回归/留出答案，不调用桥、设备或模型。`tests/fixtures/policy-preview/` 是生产 preview 的公开运行资源，部署时必须携带，不能当作可删除的临时测试目录。

- 唯一规范内容对象固定为 `{base:{version,hash,files},pack:{hash,config,files},data:<envelope 投影>}`；`H(content)` 必须等于 `prompt_hash`，协议回显、分区消息和 preview 元数据只作外层包装。
- 该完整内容对象 ≤64 KiB（同时保留实际发送 system/developer/user 文本上限）；渲染前从已加载四载荷原文重派生包结构，篡改内存派生的短语/分档不会被接受为同一摘要的不同策略内容。

- ENVELOPE 全部键必填、拒绝额外键；`nonce` 为规范 UUID，摘要为 64 位小写十六进制，完整 envelope 按规范 UTF-8 不超过 2048 字节，超限 `input_too_large`，不截断原文。HR 的 `conversation_key` 必须是非空的 `boss:conv:<64hex>`；职位 `job_brief` 必须非空；按已采纳裁定，另一 subject 键是 string|null，允许合法空字符串，anomaly 两个 subject 键可为 null/空。
- 构建器按显式 now 过滤事实：只保留注册表、包 `allow_keys`、`source=user_confirmed` 且未过期（`expires_at<=now` 丢弃）的候选；同一 key 重复或值/有效期冲突时整键受控丢弃、留给待办，不做同值去重；其它独立有效键保留。公开 Envelope 校验仍拒绝已到期事实、非法结构和重复 key，不通过放松 validator 掩盖构建器过滤。
- Decision 的前三个标识逐字回显；`evidence` 必须是 inbound 非空原样子串；`reply` 0–300 码点、只允许 LF；程序非空追加披露后复验完整码点数、联系方式、`never-say`、额外禁谈主题与有限事实来源模式，超限/冲突不截断。当前已识别“我有 N 年/我在某地/期望薪资 N”等形式时，必须匹配同键已确认值；未识别语义不能因此放行，最终仍按 C 待办。
- 租约有效期不使用 Envelope 新字段：由调用方传入可信 `lease_expires_at`（M1 `Lease.expires_at`），`now>=lease_expires_at` 拒绝。M4a preview 不生成 Decision，也不证明模型语义安全。
- 有限正则未命中不等于语义安全；v1 无独立 A 档前置判据时程序前置档位固定 C，最终档位再与包分档及硬下限取最严格者。

### 7.4 列表、快照与恢复

```powershell
python -m app policy list
python -m app policy snapshot --pack prompts/local/my-profile
python -m app policy restore --snapshot prompts/local/history/<snapshot-id> --dest prompts/local/my-profile-rollback
```

- `list` 分别列出 local 直接活动包与 history 快照的相对路径、profile_id、base/pack 摘要与有效性；不输出正文、不自动选最新，history 不是活动包，坏包只标无效。链接/junction 候选保留相对路径并标 `valid=false`，不跟随、不递归；读取边界仍拒绝链接。
- `snapshot` 把已校验四载荷与可选双指南复制到 `prompts/local/history/<UTC时间-随机后缀>/`，旁边写 `<snapshot-id>.local.json`；清单固定 `{schema_version,profile_id,source_pack,base_version,base_hash,pack_hash,created_at}`。复制前后不仅复核暂存副本，还重读活动源四载荷与指南，源变化或读取失败受控 `policy_changed` 并清理本次自有暂存/发布；清单与副本全部完成才发布成功，不覆盖已有 ID。
- `restore` 要求清单 `pack_hash`、快照重算 `pack_hash`、目标重算 `pack_hash` 三值完全相等，`base_hash` 与当前 base 相等、`base_version` 匹配，并保留原 `profile_id`；只恢复到新的 local 直接子目录，目标存在报 `policy_exists`，不覆盖原包。先写点前缀暂存并完整校验，再同卷改名；中断或失败不暴露半包。

阶段日志：`policy` 命令除命令级 `started`/终态外，还在实际执行层用同一 `ProcessLog`/correlation 记录 `load`、`validate`、`render`、`publish` 的 `started` 与 `ok/failed/undetermined`、固定原因和耗时；恢复活动状态未知记 `undetermined`。日志写入失败/降级不改变业务输出、状态或退出码；不记录正文、事实、私有路径或异常原值。

M4a 尚无 M4c 的真实运行/停止/未决租约检查，生产 CLI `restore` 固定受控拒绝为 `policy_changed`，`errors` 指向目标冲突，不把未知状态当 `stopped`、不用占位 `True`。只有测试可向同一生产 `restore_pack` 用例注入明确合成 `stopped` 证据，验证文件事务与三值一致；完整 CLI 安全接线与真实恢复须在 M4c 另行复验，没有公开 CLI 绕过参数。

恢复后必须重新 `validate`、`preview`，以后还要按策略包说明 §5–6 重新 eval/取得运行授权；快照、公开示例通过或 preview 都不构成发送授权。恢复不重置同 profile 的额度/冷却，不回滚数据库、动作证据或已发送内容。

### 7.5 策略错误外层

`policy` 成功输出四键：

```json
{"schema_version":1,"status":"ok","reason_code":null,"result":{}}
```

失败也输出四键：`{schema_version:1,status:"failed",reason_code:<固定>,errors:[{file,field,line,code}]}`，`errors` 可为空；`policy` 参数形状错误、全局配置受控错误或配置读取意外异常同样使用四键（配置错误保留 `invalid_config` 原因并以空 `errors` 表达），不改变其它命令的三键约定。`operation` 只按命令位置识别，配置值含 `policy` 不改变外层。原因码固定 `invalid_input/invalid_policy/policy_exists/policy_io_error/policy_changed/unknown_case`；配置错误沿用全局 `invalid_config`。字段诊断码固定 `missing/unknown_key/duplicate/type/range/vocabulary/coverage/safety_floor/encoding/path/size/sensitive/conflict/format`。`file` 仅为项目内相对路径或 null，`line` 为 1 起整数或 null；未知短语标题等不可信值只定位固定父字段或 null，不得回显原值、可疑键名或底层异常。帮助退出 `0`，业务/校验/IO 失败退出 `2`。

## 八、显式真实设备模式

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

按计划 M0–M3 没有业务设备循环；当前已实现 M0 探针、M1 本机状态库、M2 合成传感器与 M3 离线读取，真实探针只是一次独立调用。M3 起真实读取/采集必须纳入共享 OS 设备门禁与设备用例全局占用；M4 门禁未交付前 `read` 不提供真实 `--device` 入口，也不得与设备循环并用。当前仓库尚未对任何真实设备执行过该模式，M2 合成 profile 与 M3 合成读取都不代表真实版本已标定或真机只读一轮通过。

## 九、输出与退出码

`doctor` 业务结果固定包含以下七个键：

| 键 | 含义 |
|---|---|
| `schema_version` | 固定整数 `1` |
| `status` | `ok` 或 `failed` |
| `reason_code` | 成功为 `null`，失败为固定原因码 |
| `device_count` | 设备清单条数 |
| `selected_alias` | 所选逻辑别名或 `null` |
| `unlocked` | `true`/`false` 或 `null`（未知） |
| `foreground_app` | `boss`、`other` 或 `unknown` |

`doctor` 业务失败原因码：

| 原因码 | 含义 |
|---|---|
| `device_disconnected` | 无设备、ADB 缺失、超时、非零退出或 serial 不在清单 |
| `device_ambiguous` | 多台设备且未选择 |
| `device_offline` | 设备离线或清单状态未知 |
| `device_unauthorized` | 设备未授权 |
| `device_locked` | 锁定或解锁状态未知 |
| `wrong_page` | 前台不是 BOSS 或无法判定 |

`sense` 成功或业务停止时输出 `{schema_version:1,result:<ReadResult>,stop_reason:<原因或 null>}`；`result` 的所有键必填、拒绝额外键，`stop_reason` 只取 `null/unread_unverified/needs_targeted_verification` 或读取失败/未决原因。`stop_reason=null` 退出 `0`，其它退出 `2`。参数、配置、夹具读取或仓储错误不输出业务字段，而是输出三键安全对象（解析器已构造的 ReadResult 失败/未决除外，它们保留完整结果且不写库）。

`read` 输出 `{schema_version:1,result:<ReadResult>,actionable:<bool>,gate_reason:<null|fixed>}`；`result` 与 M2 同样保留完整七键，`gate_reason` 仅 `not_ok/field_unknown/identity_ambiguous/incomplete/no_target`。`status=ok` 且 `actionable=true` 退出 `0`，其它退出 `2`；失败/未决的 `value` 必须为 null。`read` 不写库，因此没有仓储错误出口。

`policy` 使用第七节 7.5 的四键成功/失败外层，与 doctor/sense/read 的三键安全对象不同；这是策略命令的明确扩展，调用方应按命令读取对应外层。

参数、配置、夹具或 M1/M2 仓储错误不输出业务字段，而是输出三键安全对象：

```json
{"schema_version":1,"status":"failed","reason_code":"invalid_input"}
```

输入类原因只取 `invalid_input`、`invalid_config`、`invalid_fixture`；M1 仓储原因取 `invalid_input`、`lease_busy`、`stale_lease`、`owner_not_stopped`、`clock_changed`、`store_busy`、`store_io_error`、`db_version_unsupported`、`migration_failed`。所有失败退出 `2`；成功退出 `0`；帮助退出 `0`。程序不使用退出 `1` 表示可重试失败。

若业务 JSON 的 stdout 写入失败（`UnicodeError`/`OSError`），程序安全退出 `2`，不回显 traceback，也不要求再次写 JSON；日志会记录 `output=failed` 并以 `internal` 收尾。`--help` 仍由 argparse 直接处理、正常退出 `0`，不套用业务 JSON 契约。本机日志降级不是业务失败，退出码仍按业务结果；stderr 只多一行固定 `m0_process_log_degraded`。

## 十、fixture 输入规则

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

M2 `sense` 的合成输入另用 `tests/fixtures/sensor/` 的 manifest/profile/XML 契约，不复用 M0 doctor 夹具。manifest 引用只能指向同夹具目录内的既有 `profile.json`/XML；解析器不读取仅供测试的 `expected.json`。M3 `read` 另用 `tests/fixtures/read/` 的 manifest/profile/XML 与可选像素标定，同样不读取 expected；它不消费 M2 sensor 夹具，也不把合成 profile 升级成真实标定。M4a `policy preview` 固定读取 `tests/fixtures/policy-preview/<case>.json`，案例 schema 见第 7.3 节和目录指南；该目录是生产公开资源，不含期望/答案，必须随部署携带。M1 的 ReadResult/Evidence 由测试或 M2 构造，M1 没有 CLI 文件入口，不要把 M0 fixture 形状当作 M1/M2/M3 快照输入。

## 十一、本机过程日志与复核

每次 `python -m app doctor`、`store init`、`sense`、`read` 或 `policy` 运行都会创建一个独立的 `data/logs/m0-process-<运行 UUID>.jsonl` JSON Lines 过程日志；不同进程/命令不共享同一个追加文件，避免并发丢行或互相覆盖。该目录被根 `.gitignore` 的 `/data/**` 覆盖，不提交 Git；不调用网络、不启动后台任务，也不写远程日志。旧的共享 `m0-process.jsonl` 若存在仅为历史文件，当前代码不再写入。

日志器对显式 `ProcessLog(path=...)` 同样采用隔离语义：传入路径只作为同目录命名基础（目录或文件均可），实际文件名由唯一日志器追加运行 UUID 后决定；不会退回共享追加。需要读取时使用日志器给出的实际路径，而不是假定等于传入路径。

每行固定包含 `schema_version/ts/correlation_id/stage/operation/result/reason_code/error_category/duration_ms/command/exit_code/mode/config_source/device_count`：

- `stage` 固定为 `start/arguments/config/mode/fixture/probe/adb_devices/adb_window/decision/store/sense/read/policy/adb_hierarchy/adb_screen/adb_evidence/output/exit`，可用同一 `correlation_id` 串联参数、配置、fixture/探针、ADB 命令与证据保存、sense 解析、M3 read 解析/像素/合并/门禁、M4a policy 文件阶段、业务判定、store 初始化/核验与 upsert/unresolved、输出和出口。
- `operation` 仅在 `store` 阶段取 `init/open_store/upsert_snapshot/claim/start/renew/finish/recover/unresolved_conversations`，`sense` 阶段取 `parse/aggregate/stop_reason`，`read` 阶段取 `load/page/job_list/job_detail/unread/conversation/merge/pixel/gate`，`policy` 阶段取 `load/validate/render/publish` 及命令名 `init/validate/preview/list/snapshot/restore`，其它阶段为 `null`；公开仓储操作先记 `started`，再记 `ok` 或带固定原因码/`error_category=store` 的 `failed`，并写耗时；`sense/parse`、`read/load` 与各 read 操作按 `ReadResult.status` 记 `ok/undetermined/failed`，内部异常记 `failed/error_category=internal` 后继续公共安全出口；`policy` 在真实执行边界分别记录阶段 `started` 与终态，命令级再记 `started`/终态，恢复活动未知记 `undetermined`；不记 PID/host/nonce/键/正文/XML/SQL/文件内容/路径/异常。
- `result` 固定为 `started/ok/undetermined/failed`；`reason_code` 沿用计划词表（含 `identity_ambiguous`、字段未知/像素原因与 gate 原因）与 M4a `invalid_policy/policy_exists/policy_io_error/policy_changed/unknown_case`；`error_category` 固定为 `argument/config/fixture/probe/business/store/sense/read/policy/pixel/gate/evidence/internal/adb_missing/adb_timeout/adb_nonzero/adb_oserror`。
- `command` 只写 `adb_devices/adb_window/adb_hierarchy_dump/adb_hierarchy_cat/adb_screencap`；每次命令先记 `started`，完成后再记 `ok` 或固定失败类别。
- `store` 阶段先记 `started`，再记 `ok` 或带固定仓储原因与 `error_category=store` 的 `failed`；非预期异常只把失败事件记为 `reason_code=null`/`error_category=internal`，业务 stdout 仍为 `store_io_error`/退出 `2`，最终 exit 分类与失败事件分类分别记录、互不替代。不写数据库路径。
- `output` 只为 JSON 收尾：写出成功记 `ok`，写出失败记 `failed`（`error_category=internal`）并安全退出 `2`；不能据此假称 JSON 已输出。
- 不写参数原值、serial、窗口原文、数据库路径、SQL 参数、异常消息、绝对路径或个人事实。
- 日志是尽力而为的辅助产物：调用端忽略单次 `record` 的 `False`，写入失败时设置内部 `degraded` 标记；唯一 `emit_degraded_warning` 每次运行最多向 stderr 写一次固定 `m0_process_log_degraded`，CLI 与仓储 API 共用、不双报，stdout JSON 与退出码不变，也不伪造成功行。告警出口自身失败也不改变业务结果。没有日志行不等于命令成功；该标记表示该次日志不完整，不能声称磁盘失败时完整日志已保存。

复核方式（按修改时间取最近一次运行；每次运行只有一个完整文件）：

```powershell
$log = Get-ChildItem data/logs/m0-process-*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content $log.FullName
git check-ignore data/logs/m0-process.jsonl data/runtime/m1-acceptance.sqlite3 data/runtime/m2-acceptance.sqlite3
```

已知某次 `correlation_id` 时可跨文件过滤：

```powershell
Select-String -Path data/logs/m0-process-*.jsonl -Pattern '"correlation_id":"<UUID>"'
```

stdout 不包含 `correlation_id`。按单次运行核对时，优先读取该次独立的 `start`→`exit` 文件；同一 `correlation_id` 不应跨两个运行文件。fixture 模式仍保持零设备、零网络、零子进程和零业务库；`store init` 只写项目内本机 SQLite；`policy` 只写项目内 `prompts/local`/history 与本次日志。日志按运行累积且没有自动轮转/保留期，需要时由使用者在本机清理旧文件。

## 十二、尚未实现

M3 离线读取（`read`）与 M4a 策略包六命令（`policy`）已实现；M4b 真实桥/eval、M4c 真实活动/未决检查与完整 CLI 安全恢复、M5 的动作护栏以及 M6–M8 的调度、清理和灰度都尚未实现。M1 的完整任务租约 API 已实现，但 M2 `sense` 只调用 upsert 与未关闭会话查询，M3 `read` 不调用仓储、不 claim/start/finish、不关闭发现。M4a `policy preview` 不生成 Decision、不调用模型；生产 `policy restore` 固定受控拒绝。真实读取、进入会话、滚动、返回与截图仍依赖后续共享设备门禁和本轮确认；不要用 M0 的 fixture 结果、M1 的合成库、M2 的合成 profile、M3 的合成读取或 M4a 的 preview 推断真实设备/模型支持范围，也不要把合成候选、探针成功、读取 actionable、`store init` 成功、策略 validate/preview 或快照当作发送授权或真实版本已标定。

## 十三、安全与隐私

- fixture 只用合成别名与状态，不放真实序列号、聊天、联系人、公司、截图或绝对路径。
- 真实模式输出不含真实序列号、窗口原文或异常命令参数。
- M1 只保存摘要、键、状态与白名单事件，不保存聊天正文；M2 只解析合成 XML，日志不写 XML、身份键或正文。M3 `read` 的会话/职位正文只存在内存与合成 CLI 数据区，不写日志、事件或数据库；像素标定与原始截图只在合成测试与本机证据目录使用。M4a `policy` 只读写四载荷、双指南、history 与固定 preview 案例；日志不写文件正文、绝对路径或异常，preview/快照输出属于本机数据，不提交公开报告。数据库、事件、日志与截图只在本机保存，不提交 Git。
- 不读取根 `config.json`、环境文件、凭据或个人事实。
- 设备、日志、截图和评测产物只在本机保存，不提交 Git。
- 发现疑似敏感信息时按根 `AGENTS.md` 的“开源项目敏感信息告知”处理，不在对话或公开材料中复述原值。
