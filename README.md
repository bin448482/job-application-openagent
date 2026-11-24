# job-application-openagent

面向开源维护的 Android 求职助手项目（重新设计中）。

## 当前状态

旧的业务源码、Skills 与业务离线测试已清理，项目正在分阶段实现。当前已实现 M0 只读探针、M1 本机状态库/租约、M2 合成未读四源传感器、M3 离线读取阶梯/页面/职位/会话读取、M4a 策略包离线编辑/校验/预览/快照/恢复、M4b A/B 本机决策桥/分层 policy eval/留出冻结登记、M4c profile 独立事实库/本机事实确认入口、固定 OS 共享 SQLite 设备门禁、真实 restore 活动检查、验收凭证复核与 `run check` 预检，以及 M5 五动作护栏/唯一文本额度冷却/固定人工卡/受控对账；仍未连接真实设备或模型，并已建立独立的 promptfoo 合成黄金测试集：

- `pyproject.toml`、`requirements.txt`：Python 业务元数据；暂不引入第三方 Python 依赖。
- `app/`：M0–M5 分层程序，提供 `python -m app`、只读 `doctor`、`store init`、合成 `sense`、合成 `read`、`policy`（含 `eval`/`holdout`/`acceptance`）、本机 `facts`、`run check` 与 `action`（perform/eligibility/card/reconcile），以及 `domain/` 纯模型/读取/策略/评测/事实/动作契约、`adapters/sqlite/` 本机仓储/profile 库/共享设备门禁/活动检查、`adapters/boss/` 解析、`adapters/adb/` 采集与动作原语、`adapters/files/` 策略/评测文件、`adapters/llm/` A/B 子进程桥与 `adapters/simulated/` 合成动作执行器。
- `tests/`：M0–M5 标准库 `unittest` 与纯合成夹具。
- `docs/operator.md`：M0–M5 探针、状态库、合成传感器、离线读取、策略包、桥/eval/留出、profile 事实、共享门禁、恢复检查、验收复核、`run check` 预检与动作护栏/对账说明。
- `docs/BOSS直聘_M3读取阶梯与最小数据契约_20261124.md`：M3 读取值类型、字段阶梯、像素标定、跨屏稳定消息身份与失败/门禁契约。
- `docs/BOSS直聘_M5动作护栏与对账_20261124.md`：M5 动作键、profile v2 备份迁移、分动作授权、文本额度/冷却、观察→三态证据、人工卡/受控恢复与离线验收边界。
- `prompts/`：公开 base、纯合成示例与 `policy init` 生成的本机策略包/history；`tests/fixtures/policy-preview/` 是 preview 的公开合成输入资源；`evals/policy-suite/` 是 eval 的公开套件与旧 24 映射。
- `config.example.json`：可提交的示例配置，当前只描述未来 M4 的策略包引用，不是 M0 配置。
- `data/`、`screenshots/`：本机运行产物目录，只提交目录说明；`data/runtime/` 中的 SQLite 库与 `data/evals/` 报告被忽略。
- `.agents/AGENTS.md`、`.claude/AGENTS.md`：工作流入口占位，内容待设计。
- `AGENTS.md`、`CLAUDE.md`：开发约束与目录索引。
- `evals/`、`promptfooconfig.json`：24 个记忆与授权决策案例、评测提示词、断言和运行入口。
- `package.json`、`package-lock.json`：锁定 promptfoo 评测开发依赖，不改变 Python 业务依赖。

## M0 只读探针（已实现）

在项目根目录执行：

```powershell
python -m unittest tests.test_doctor
python -m app doctor --fixture tests/fixtures/doctor/ready.json
```

第二条命令退出 `0`，只向 stdout 输出一个 UTF-8 JSON：

```json
{"schema_version":1,"status":"ok","reason_code":null,"device_count":1,"selected_alias":"primary","unlocked":true,"foreground_app":"boss"}
```

fixture 模式只读合成输入，不启动子进程、不连接设备、不调用网络、不写业务库；每次运行只创建自己的被忽略 `data/logs/m0-process-<运行 UUID>.jsonl` 并写固定字段过程日志，运行之间不共享追加，写入失败不影响 stdout 或退出码，但 stderr 会写固定 `m0_process_log_degraded` 表示日志不完整。配置只有在显式传入全局 `--config <相对文件>` 时读取；省略与 `{}` 都取 `{"schema_version":1,"mode":"read_only"}`，不会自动读取根 `config.json`。配置与夹具在共享 JSON 输入边界拒绝重复对象键（含嵌套），夹具还拒绝不可 UTF-8 编码的字符串，错误只输出安全 JSON 且不回显原值。

真实探针必须显式指定：

```powershell
python -m app doctor --device --serial <serial> --confirm-device
```

执行前必须说明两条只读命令、预计最多 10 秒、设备独占要求并取得本轮确认。`--confirm-device` 是本轮确认入口；未确认时在启动任何 ADB 子进程前拒绝。真实模式先取得固定 OS 位置共享 SQLite 设备门禁的唯一 primary 占用，每条 ADB 命令前复核，正常结束只在该次探针已明确收敛时释放；异常或未知保留占用并隔离。输出不含真实序列号、窗口原文或门禁路径。当前尚未在真实设备上执行该模式，M0 也不提供业务循环、导航、截图或发送。完整规则见 [docs/operator.md](docs/operator.md)。

## M1 状态库与租约（已实现）

创建或核验本机 SQLite v1 库：

```powershell
python -m unittest tests.test_store tests.test_migrations tests.test_process_log_concurrency
python -m app store init --db data/runtime/m1-acceptance.sqlite3
```

第二条命令退出 `0`，只输出固定 JSON：

```json
{"schema_version":1,"status":"ok","reason_code":null}
```

连续执行两次都成功，第二次只核验结构、不改变业务行。`--db` 省略时为 `data/runtime/runtime.sqlite3`；只接受项目内相对路径，解析链接和 `..` 后越界即 `invalid_input`。首次初始化在一个事务内创建五张 STRICT 表（`hr_conversations`、`hr_discoveries`、`hr_messages`、`device_lease`、`events`）、全部约束、外键与索引并设置 `user_version=1`；中途失败整体回滚。已存在完整 v1 库不得覆盖重建；未知版本、非空无版本或结构篡改都拒绝为 `db_version_unsupported`。

M1 仓储已实现合成快照入库、`claim/start/renew/finish/recover`、全局 `primary` 设备租约与未关闭会话查询；只允许 `app=boss`。任务 TTL 为 60 秒，仅 TTL 到期不会释放设备占用；`blocked` 会清任务租约但保留设备原身份并置 `quarantined`，下一次 claim 仍为 `lease_busy`。恢复要求已过期、显式确认、本机 host 匹配且注入的停止检查器判定 `stopped`。所有写操作同事务，失败整体回滚。完整 schema、状态机与错误码见 [docs/operator.md](docs/operator.md)；`data/runtime/` 被 Git 忽略，不得提交数据库或事件。

## M2 合成未读传感器（已实现）

在项目根目录执行：

```powershell
python -m unittest tests.test_sensor tests.test_sensor_store
python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3
```

第二条命令退出 `0`，只输出一个 UTF-8 JSON（`snapshot_id` 每次运行不同；下列为一次合成正例的实际输出）：

```json
{"schema_version":1,"result":{"status":"ok","value":{"app":"boss","profile_id":"synthetic-boss-m2","coverage":"viewport","signals":{"row_badge":{"state":"present","count":1,"reason_code":null},"new_greeting":{"state":"unknown","count":null,"reason_code":"missing_signal"},"tab_badge":{"state":"unknown","count":null,"reason_code":"missing_signal"},"preview_delta":{"state":"unknown","count":null,"reason_code":"no_baseline"}},"rows":[{"conversation_key":"boss:conv:1c17a032819302d025dd2131155a2e6340c6b3a5b2f4c5ad0f443e67da3f9928","identity_hash":"1c17a032819302d025dd2131155a2e6340c6b3a5b2f4c5ad0f443e67da3f9928","preview_hash":"868b93e90b756be73450d807a4cb8a92a7a646a7dca99d0aaba45e4d185cb5fb","time_hint":"10:00","unread_count":1}],"candidates":[{"candidate_key":"boss:discovery:1de577f9bc73b948e17470f50b7c739954d53c48c6ce009e778dac63ec162540","conversation_key":"boss:conv:1c17a032819302d025dd2131155a2e6340c6b3a5b2f4c5ad0f443e67da3f9928","kind":"unread","source_names":["row_badge"],"fingerprint":"96104d444f0f0c97bf61e659810318baff3949d2655af9881fb60144605b3744"}],"unread_state":"present"},"reason_code":null,"snapshot_id":"00000000-0000-4000-8000-000000000101","observed_at":"2026-11-24T00:00:00.000Z","source":"fixture","confidence":"high"},"stop_reason":null}
```

正例 manifest 只有一页合成 XML、区域内角标 `1`、无基线；因此产生唯一 `unread` 候选，`stop_reason=null`。重放同一夹具不会增加发现行或事件。`--db` 省略为 `data/runtime/runtime.sqlite3`，只接受项目内相对路径；只有 manifest/profile/XML 全部通过且合并 `status=ok` 后才初始化/打开库，先 `upsert_snapshot` 再查询未关闭会话并聚合。

M2 只读合成 profile/manifest/XML：不支持设备、网络、模型或子进程，不 `claim/start/finish`、不打开会话、不推进已读基线，也不把合成 profile 标为已标定真实版本。`stop_reason` 非空时退出 `2`，用于表达 `unread_unverified` 或 `needs_targeted_verification`；读取本身失败/未决时保留完整 `ReadResult` 且不写库。manifest/profile 字段、四源规则、分页重叠、覆盖率、原因码与夹具布局见 [docs/operator.md](docs/operator.md) 与 [tests/fixtures/sensor/AGENTS.md](tests/fixtures/sensor/AGENTS.md)。

## M3 离线读取阶梯（已实现，未真机验收）

在项目根目录执行：

```powershell
python -m unittest tests.test_read tests.test_screen
python -m app read --fixture tests/fixtures/read/page.json
python -m app read --fixture tests/fixtures/read/job_list.json
python -m app read --fixture tests/fixtures/read/job_detail.json
python -m app read --fixture tests/fixtures/read/unread.json
python -m app read --fixture tests/fixtures/read/conversation.json
```

`read` 只读合成 M3 manifest/profile/XML 与可选显式合成像素标定，不连接设备、数据库、模型或网络，不 `claim/start/finish`、不关闭发现、不写正文或 `verified` 基线。输出固定外层 `{schema_version:1,result:ReadResult,actionable:bool,gate_reason:...}`：`ok` 但字段未知/身份未决时 `actionable=false` 并退出 `2`；`failed/undetermined` 的 `value=null`。读取值类型包括 `PageSnapshot/JobList/JobDetail/UnreadRead/Conversation`，并继续复用唯一 `ReadResult` 三态/来源/原因外层。

M3 的层级默认优先，仅字段未决时才用显式标定像素；视觉默认关闭，无独立交叉证据不放行；前级 `failed` 不降级掩盖，跨级冲突转 `undetermined`/人工；像素/人工来源与置信度不得冒充层级高置信，结果状态/计数必须与证据联动。跨屏消息身份与正文内容指纹分离：只有 profile 显式验证的应用消息稳定标识才能生成 `boss:msg:` 入站键；同正文、同秒、屏幕序号、坐标或布局 resource-id 都不构成稳定身份；重复稳定身份或重复正文/方向控件会使会话入站未知。会话身份必须与实际标题及已保存发现上下文重新核对，不能用 manifest 自报键。详细契约见 [M3 契约文档](docs/BOSS直聘_M3读取阶梯与最小数据契约_20261124.md)。

真实采集适配器已按参数数组、`shell=false`、5 秒超时、不重试并有本机证据保存实现，但只在测试中 mock `subprocess.run` 验证。共享 OS 设备门禁（M4）未交付前不提供真实 `read --device`，不得用 profile 内 `primary` 任务锁替代。真机像素阈值/区域/字体/分辨率、真实消息稳定标识、真实 ADB 输出与单台真机只读一轮均未执行，M3 不代表真机标定或里程碑真机通过。

## M4a 策略包离线编辑与回滚（已实现，未接模型/设备）

在项目根目录执行：

```powershell
python -m unittest tests.test_policy_contract tests.test_policy_pack tests.test_policy_store
python -m app policy init --dest prompts/local/demo
python -m app policy validate --pack prompts/local/demo
python -m app policy preview --pack prompts/local/demo --case policy-greeting
python -m app policy list
python -m app policy snapshot --pack prompts/local/demo
python -m app policy restore --snapshot prompts/local/history/<snapshot-id> --dest prompts/local/demo-rollback
```

M4a 交付公开 `prompts/base/` 两份 base、`prompts/examples/default/` 全 C 空事实示例、固定 `tests/fixtures/policy-preview/` 独立合成 preview 输入，以及完整 DecisionEnvelope/Decision 严格契约、TOML+三 md schema、45 组合唯一覆盖与统一硬下限（2 个可 A、4 个至少 B、其余 39 个固定 C）、四类无自引用摘要、唯一 `{base,pack,data}` 内容对象、system/developer/user 固定投影和披露/事实复验。`validate` 输出有效限制、分档、硬下限、来源与摘要；`preview` 只做纯渲染，不读黄金答案、不调用桥、设备、网络或模型；`init` 只建 `prompts/local` 直接子目录且不覆盖；`snapshot` 复制前后重读活动源并保证清单与副本完成；`restore` 要求清单/快照/目标三值 pack_hash 相等、base 匹配并保留原 profile_id，暂存后同卷发布、不覆盖旧包。策略命令的成功/失败四键外层、配置错误四键、`load/validate/render/publish` 阶段日志与恢复受控拒绝见操作手册。

策略命令成功/失败使用四键外层（失败带 `errors:[{file,field,line,code}]`），这是对既有 doctor/sense/read 三键外层之外的明确扩展。M4c 已把生产 CLI `restore` 接到真实活动检查：共享设备门禁存在任意占用、profile 库 leased/running 时拒绝为 active，blocked/坏库/绑定不符时保守为 unknown，均以 `policy_changed` 拒绝且不通过换目录解除占用或重置额度；只有 stopped 才允许恢复，恢复仍要求清单/快照/目标三值一致。M4b 桥/eval 已单独实现于下一节；真实设备执行、事实授权与真实 provider 能力仍不由 M4a/M4b 证明；preview/snapshot 通过不构成发送授权。完整命令、字段、错误码与回滚边界见 [docs/operator.md](docs/operator.md)。

## M4b 决策桥与真实策略评测链路（已实现，未调用真实模型）

```powershell
python -m unittest tests.test_bridge tests.test_policy_eval
python -m app policy eval --help
python -m app policy holdout register --batch-id <批次ID> --version <版本> --sample-count <数量> --annotator <标注者> --maintainer <维护者> --candidate-release <候选>
python -m app policy holdout retire --batch-id <批次ID>
```

M4b 交付架构 A 外部 agent 与 B 本机进程两个参数数组桥：固定 `system/developer/user` 三消息、独立协议回显、`tools=[]`、`history=[]`，`shell=False`、`stderr=DEVNULL`、20 秒总预算，stdin/stdout 用本机系统临时目录的 `TemporaryFile` 承载并在返回时删除，运行中每 20ms 监测输出增长、超 `64KiB` 立即 kill，结束后再查一次并只读 `64KiB+1` 哨兵；拒绝角色声明不符/坏 JSON/坏编码/超大输出。超时/超限 kill/回收直接子进程，不留读取线程；若桥自行派生短命后代并继承句柄，不推断所有后代已退出。只有传输层可证明进程未启动时重试一次；重试前重读时钟/租约/包，提交后超时/schema/坏回显/过期一律丢弃且不重发。决策用例桥前、重试前、桥后重读书籍化时钟、可信租约与当前 base/pack，复用唯一 `parse_decision`/`validate_decision`/`append_and_validate_reply`，不复制业务校验器。

`policy eval` 实际加载 `--pack` 指定的当前包，经同一决策桥逐例运行公开/回归/留出套件；必须显式选择 `external-agent` 或 `local-process`，拒绝 echo/参考回放，并要求 `--confirm-external-send`。该确认只表示本轮模型评测外发与计费，不授予设备、事实写入或业务发送权限，也不替代留出无上传合同。逐例报告只写本机 `data/evals`，绑定 base/pack/profile/provider/model/model_config_hash/suite_hash 与 process correlation；失败、零样本、缺私有套件、非公开套件或 simulated transport 不生成有效验收凭证。公开套件与旧 24 案例 npm 回归独立，`evals/policy-suite/mapping.json` 只登记每例责任与未接入原因。留出按语义输入指纹占用历史样本，已看样本不得换批次/候选/套件或拆子集重冻结；访问意向在调用前锁内持久化，完成/失败/中断/重复如实区分；退役整批写 `data/evals/regression-admissions/` 副本并可被同一 regression 路径实际运行；登记写失败保留未确认准入本机残留，但 registry 过滤使其不可运行，不删除其它 owner 文件。public/holdout 不读取无关 regression 准入；重试前用唯一 render 校验器复核事实有效期；临时文件创建/输入写也计入总预算，输出监测/等待 OSError 受控。留出还要求 `data/evals/holdout-attestation.local.json` 绑定当前 model/config 并显式声明 no-upload/cache/share/telemetry/cloud-fallback 全部关闭；该声明是 operator/host 合同，不证明子进程真实无网络。

真实 provider、私有回归/留出与真实 M5 动作未运行；桥自报角色/工具/历史只是协议声明，不证明第三方实际能力。详见 [docs/operator.md](docs/operator.md) 与 [evals/policy-suite/AGENTS.md](evals/policy-suite/AGENTS.md)。

## M4c profile 事实、共享设备门禁与 M4 闭环（已实现，未接真机/真实 provider）

```powershell
python -m unittest tests.test_profile_facts tests.test_device_gate tests.test_run_guard tests.test_profile_session_lease tests.test_m4c_closed_loop
python -m app facts set --profile demo --key work_location --value "<合成值>"
python -m app facts set --profile demo --key work_location --value "<合成值>" --confirm
python -m app facts show --profile demo --pack prompts/local/demo
python -m app facts revoke --profile demo --key work_location --confirm
python -m app facts forget --profile demo --key work_location --confirm
python -m app policy acceptance check --pack prompts/local/demo --credential data/evals/eval-<id>/acceptance.json
python -m app run check --pack prompts/local/demo --mode read_only --confirm-device
```

`facts` 使用本机交互 CLI 逐字段录入长期事实：未带 `--confirm` 只输出提案，不写事实；确认后从库中重读并再次显示值。每条事实由程序生成来源 ID、记录确认时间与有效期；同键多个 active 来源不按较新时间自动择一，而是转 `conflict` 待办；显式 `--replace` 才作废旧来源。撤销保留历史来源，`forget` 删除当前事实行并只保留来源 ID/无值审计，明确不宣称擦除 SQLite WAL 或既有本机备份。事实值只写 `data/runtime/profiles/<profile_id>/runtime.sqlite3`，不进入策略包、过程日志、公开报告或错误文本；带 `--pack` 时 pack.profile_id 必须等于 `--profile`，否则固定 `profile_mismatch`；路径查询只有明确 `FileNotFoundError` 算缺失，目录/非普通文件/权限错误报 `profile_io_error`，不伪造空结果。不读取 HR 文本、旧合成库、根 `config.json` 或凭据。旧 `data/runtime/runtime.sqlite3` 不自动导入；合法 profile 库在完整校验后由唯一 `SqliteStore` 复用 M1 任务能力，旧普通 M1 库验证不放宽；profile 模式写入前拒绝另一 profile 的合法快照。

共享设备门禁固定在 Windows `LOCALAPPDATA/job-application-openagent/device-guard.sqlite3`，不随仓库、worktree、clone、`--pack` 或配置改变；其它平台在支持前拒绝真实运行。整个真实会话（含 read_only）先 `BEGIN IMMEDIATE` 条件插入唯一 `primary` 行，存在任何状态即拒绝；acquire 保存完整 owner/pid/host/nonce/profile/路径，启动、续租与每条设备命令前复核完整身份与绑定。release 在写事务内再次比较同一完整身份，任一 owner/profile/路径变化都不删除原占用。只允许明确新空库初始化；未知版本、额外对象或旧占用表不改写，保留原内容。崩溃、PID 消失、TTL、库不可读、nonce 篡改或换 profile 都不自动解锁；正常释放只在该次操作明确收敛且无在途子命令时删除，异常保留原绑定并置 `quarantined`。`probe_path` 只有明确 `FileNotFoundError` 才算缺失；目录/不可读/权限错误等存在但不可判定路径返回 unknown，restore 不发布。M0 `doctor --device` 已纳入同一占用；M3 真实采集适配器缺 gate 时在零子进程处拒绝，并在每条子命令前复核，未取得门禁或未确认时任何 ADB 子进程都不启动。

`policy acceptance check` 只读比较 M4b 验收凭证与当前 base/pack（可选 provider/model 摘要），任何变化以 `policy_changed` 拒绝旧凭证，不授予设备或发送权限。`run check` 是预检而非完整运行：read_only 要求本轮 `--confirm-device`，draft_only 另要求当前验收（provider/model/model_config 齐全），`auto_send_tier_a` 在 v1 固定 C 且缺独立前置判据时始终 `authorization_required`；它不探测设备、不调用模型、不执行 M5 动作，跨库失败会保留占用并隔离。门禁适配器唯一记录 acquire/verify/release/quarantine 的 started/终态，run/restore 的 profile 打开也有唯一 started/终态。真实 `ProfileSessionLease` 保存取得时 Lease/Owner/nonce，每次关联 profile 库、门禁 session identity 与任务/设备两行；门禁身份从一次已核行读取返回，任务选择复用 `validate_lease` 并匹配 app/key/nonce。跨 profile/原库/owner、复制库、门禁 nonce 桥前/桥中变化、错误任务 key/app 或桥期间 owner 变化均拒绝，桥前/桥后由 `decide` 拒绝且不重发。实际 CLI 合成桥 eval→snapshot→restore→validate/preview→eval 串行闭环已验证，三摘要一致且同 profile 既有事实/任务审计不变。M5–M8 的动作、调度、真机执行与真实 provider 仍未实现，完整 MVP 运行不可声称完成。

## M5 五动作护栏、文本资格与受控对账（已实现，未执行真实动作）

```powershell
python -m unittest tests.test_action_domain tests.test_action_store tests.test_action_usecase tests.test_action_executor tests.test_action_cli
python -m app action card --profile demo
python -m app action eligibility --profile demo --pack prompts/local/demo --mode draft_only `
  --action-kind send_text --target-key <message_key> --conversation-key <conversation_key> `
  --task-key <task_key> --credential data/evals/eval-<id>/acceptance.json `
  --provider local-process --model-id <标签> --model-config-hash <摘要> `
  --authorize send_text --confirm-action
```

M5 的五类不可逆动作是发文本、发简历、换微信、换电话、删会话；动作键精确为文本 `boss:<message_key>:reply`、其余 `<conversation_key>:resume|wechat|phone|delete`。流程固定为“同一事务写 pending 与唯一动作键锁 → 执行一次 → 由可信前后观察推导三态 → unknown/未收敛标 blocked 并隔离 → 绝不自动重试”。动作结果、任务状态与 `action_events`/`events` 一次提交；`performed` 必须由不同前后快照与动作特有变化证明，同快照、目标早已不存在、简历仅请求或联系方式槽位未变“请求中”都不算执行。点击前断连为 `not_performed`，点击后异常/断连为 `unknown`；输入框/确认框/IME/在途命令未收敛时即使未点击也保持 pending 与共享隔离。

文本额度/冷却直接由 `action_records` 计算：UTC 日期内跨 app 合计 performed+sent 文本，按 profile/app/会话冷却；任一文本 pending/unknown 阻断新发送。额度检查与本次 pending/键锁同一事务，换包、restore、重启或调低额度不清零历史；附件 `request_sent`、`not_performed`、`unknown` 不充当成功文本。`pending/unknown/cancelled` 生成固定人工卡，`confirm_performed`/`confirm_not_performed` 需本轮显式确认、当前证据与原命令停止/现场/IME 收敛，`abandon` 只关任务且不解锁。受控接管隔离占用必须在原 profile/原库身份下带 `--confirm-recovery` 并确认原 PID 已停止；正常 acquire/TTL/PID 消失不会开锁。profile 库从严格 v1 迁移到 v2 时先写不覆盖 SQLite 备份，再单事务追加动作三表；普通 M1 库、额外对象或未知版本不宽放。

`action perform` 只接受 `draft_only`、当前验收、本轮 `--authorize` 与合成动作脚本；CLI 不提供真实 ADB 动作入口，真实动作原语只由 mock subprocess 测试。合成资格为真、`policy_tier="C"` 与 `production_auto_send=false` 并列，不代表真实 A 档或发送授权。真实手机、ADB 坐标/输入法/附件名/联系方式/删除保护/恢复时序、真实 provider 与 M6–M8 调度/清理/灰度仍未验证。

## 开发环境

- Python 3.13 或更高版本。
- 评测工具另需 Node.js >=22.22.0 与 npm；promptfoo 固定为 0.126.0。
- 真机功能另需 Android SDK Platform-Tools 中的 `adb`、USB 调试和使用者确认；M0–M5 离线测试不连接设备。
- 仓库中的 `.venv/` 与 `config.json` 为被 Git 忽略的本机文件，可自行保留或删除。

M6–M8 的调度、清理、灰度与完整运行仍待实现；M4c 的 `run check` 只是预检，M5 的 `action` 只在合成执行器/临时 profile 库上验证，均不执行真实设备或动作。M3 的读取阶梯只使用合成 XML、合成 RGBA、模拟 ADB 与独立门禁测试，不代表真实设备采集或真实版本标定；M4a 只用合成 base/包/preview 与临时项目根，M4b 只用合成本机桥桩/模拟 transport，M4c 只用临时 OS 根、合成事实与进程内/跨进程模拟，M5 只用合成动作脚本与 mock ADB，不代表真实 provider 能力、真机门禁、动作安全或发送授权。数据库、日志、截图、契约、凭据和个人事实只在本机保存。

全新机器的引导、依赖边界与验收规划见[新环境自动化部署方案](docs/求职自动化_新环境自动化部署方案_20261123.md)：分别设计 eval、ADB 工具准备和经本轮确认后的手机环境检测（§8、§10.1）；工具安装不代表手机或业务就绪。安装器与手机检查器尚未实现，干净机器及手机验收均待执行；本轮仅文档，不运行 ADB 或连接手机。

## 黄金测试集

在项目根目录执行：

```powershell
npm ci
npm test
npm run eval:smoke
```

`npm test` 使用 Node 内置测试验证断言，包括错误输出反例。`eval:smoke` 通过参考 provider 回放合成答案，只验证 promptfoo 链路；两者均不请求模型、不连接设备，也不证明模型已通过黄金集。

评测真实模型时显式选择 promptfoo 支持的 provider，并按该 provider 的文档配置认证：

```powershell
npm run eval:model -- "<provider-id>"
```

该命令会将合成输入交给指定 provider，可能产生模型调用费用。当前没有默认真实模型，不能把 smoke 结果当作模型评测。结果只写到被 Git 忽略的 `data/evals/`；入口关闭遥测、更新检查、缓存及分享，不读取本机 config.json 或个人事实。

案例覆盖偏好有效性与冲突、长期更新/临时偏好/遗忘、授权范围与到期、动作防重、请求与交付区别及 HR 指令注入。所有地点、薪资和身份均为合成值。M4c 已实现 profile 长期事实确认/撤销/遗忘与 SQLite 本机存储；HR 会话运行时记忆、跨会话检索及端到端动作读写仍待实现。字段和限制见 [evals/AGENTS.md](evals/AGENTS.md)。

工具依据：promptfoo 官方 JavaScript assertion 与自定义 provider 文档。配置和执行已按固定版本在本机验证；case metadata 不进入模型提示词。

许可证：MIT，见 `LICENSE`。
