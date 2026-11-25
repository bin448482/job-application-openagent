# job-application-openagent 项目指南

## 一、项目基本信息

正式名称为 `job-application-openagent`，依据 `README.md` 与 `pyproject.toml` 的 `project.name`。根目录为本指南所在目录，运行路径相对于项目根目录解析。

本项目面向开源维护的 Android 求职辅助工作流，目前处于分阶段实现阶段。M0 只读探针、本机白名单过程日志、M1 状态模型/租约、M2 合成未读四源传感器、M3 离线读取阶梯/页面/职位/会话读取、M4a 决策契约/策略包/离线编辑回滚、M4b A/B 本机决策桥/分层 policy eval/留出冻结登记、M4c profile 独立事实库/本机事实入口、固定 OS 共享 SQLite 设备门禁、restore 真实活动检查、验收凭证复核与 run check 预检、M5 五动作护栏/唯一文本额度冷却/固定人工卡/受控对账，以及 M6 任务调度/七条件安全点/身份游标/持久预算/`stage_preempted`/实际 `run prepare|execute`/M3 读取门禁→唯一 decide 生产组合/草稿与运行时决策记忆/第六动作 `apply_job` 原始 XML 控件发现、before-done 前置与 E2E 边界/批次 completed 与 restore 活动边界/M2 queued new_message 第二级调度/操作手册隔离报告控制，以及 G1 唯一 `derive_safe_point` 真实任务身份/持久 pending/checkpoint 来源、G2 经唯一读取端口实际一次定向 M3 采集与真实预算耗时、G3 `on_boundary`/`on_started` 到达与开始分离，以及策略发布 Windows 短暂句柄的有界 rename 重试与暂存所有权/内容身份复核已实现；旧业务源码、Skills 与业务测试已清理，不恢复旧实现或个人授权。当前已有独立的 promptfoo 合成黄金集，检查记忆与授权决策；M7 清理、M8 灰度、真实设备执行器、真实 provider、`apply_job` 生产配额合同与完整业务范围仍待实现。真实动作、真实 provider、私有回归/留出、真机与完整 MVP 运行均未运行。

本仓库自身：业务项目要求 Python >=3.13，Python 依赖保持为空；评测开发工具另需 Node.js >=22.22.0、npm 和固定版本 promptfoo 0.126.0。评测使用 CommonJS、Node 内置测试和 JSON 配置，npm 依赖通过 package-lock.json 锁定。引入 promptfoo 是当前明确的评测需求，不改变 Python 标准库优先约束。

文档所描述的消费方：评测可接受 promptfoo provider ID 连接模型或自定义端点，认证由使用者按 provider 类型配置，不提供默认真实模型。README 约定未来设备功能需要 Android SDK Platform-Tools 的 adb、USB 调试及使用者确认；当前没有业务 Skill 或设备工作流，不推定宿主、机型、包名或发送授权。

主要入口与模块关系：README 提供使用概览；`python -m app` 进入 app/cli/main.py，当前分派只读 `doctor`、`store init`、合成 `sense`、合成 `read`、离线 `policy`（含 `eval`/`holdout`/`acceptance`）、本机 `facts`、`run check` 与 `action`（含 `perform`/`eligibility`/`card`/`reconcile`）。fixture 由 CLI 读取，真实探针经 app/ports/device.py 注入 app/adapters/adb/device.py，业务判定在 app/usecases/doctor.py；`store init` 经 app/adapters/sqlite/schema.py 建库/核验，仓储实现在 app/adapters/sqlite/repo.py，领域模型与键/摘要算法在 app/domain/models.py，端口在 app/ports/store.py。M2 的 `sense --fixture` 由 app/adapters/boss/list.py 解析合成 manifest/profile/XML、分页合并，经 app/usecases/sense.py 调用同一 `StorePort` 的 upsert/unresolved 和 app/domain/scheduler.py 的纯聚合/停因；只有合并 ok 后才打开本机库。M3 的 `read --fixture` 由 app/adapters/boss/read.py 解析页面/职位/未读/会话并调用 app/adapters/boss/pixel.py 与 app/domain/ladder.py，决策门禁/定向核对/JSON 在 app/usecases/read.py；采集端口在 app/ports/screen.py，真实 ADB 采集在 app/adapters/adb/screen.py。M4a 的 `policy` 由 app/cli/main.py 接线 FilePolicyRepository（app/adapters/files/policy.py）与 app/usecases/policy.py，DecisionEnvelope/Decision、包 schema/45 组合/摘要/渲染在 app/domain/policy.py，端口在 app/ports/policy.py；公开 base/示例在 prompts/base、prompts/examples/default，preview 合成输入固定在 tests/fixtures/policy-preview。M4b `policy eval` 由 app/cli/main.py 接线 ExternalAgentBridge/LocalProcessBridge（app/adapters/llm/）、FileEvalStore（app/adapters/files/evals.py）、app/usecases/decision.py 与 app/usecases/policy_eval.py；桥/快照/租约端口在 app/ports/decision.py，套件/报告端口在 app/ports/eval.py，公开套件与旧 24 映射在 evals/policy-suite。M4c `facts` 由 app/cli/main.py 接线 SqliteProfileStore（app/adapters/sqlite/profile_store.py，schema 在 profile_schema.py）、app/usecases/facts.py、app/domain/profile.py 与 app/ports/profile.py；共享设备门禁在 app/adapters/sqlite/device_gate.py、端口在 app/ports/device_gate.py，profile 活动检查在 app/adapters/sqlite/activity.py；`policy acceptance check` 与 `run check` 分别由 app/usecases/acceptance.py、app/usecases/run.py 编排；唯一仓储快照 RepositorySnapshot 在 app/usecases/policy_eval.py，真实 profile 任务/共享门禁租约适配在 app/adapters/sqlite/session.py，profile 库经完整校验后由唯一 SqliteStore 复用现有任务能力。M5 `action` 由 app/cli/main.py 接线 app/usecases/actions.py、app/domain/actions.py、app/ports/actions.py、app/adapters/simulated/actions.py 与 app/adapters/adb/actions.py；动作表/额度/对账在唯一的 app/adapters/sqlite/repo.py 内与 M1 任务同一事务边界，profile schema v2 与备份迁移在 app/adapters/sqlite/profile_schema.py。白名单过程日志由 app/process_log.py 每次运行写到被忽略的 data/logs/m0-process-<运行 UUID>.jsonl（阶段含 `store/sense/read/policy/facts/profile/device_gate/action/run/eval/decision/adb_hierarchy/adb_screen`），M1/M2 业务库写到被忽略的 data/runtime/，M4c/M5 profile 库写到 data/runtime/profiles/<id>/runtime.sqlite3，M4b 报告/凭证/留出登记写到被忽略的 data/evals/。package.json 的 npm scripts 调用 evals 的断言测试或评测入口，promptfooconfig.json 加载提示词、24 个案例和 JavaScript 断言。smoke 仅回放参考输出，model 才使用显式 provider；case metadata 不渲染进模型提示词。评测产物仅保存到 data/evals。pyproject.toml 为业务元数据，不是已实现的 CLI 或发行包。每个目录的 CLAUDE.md 仅引用同目录 AGENTS.md，全局规则只在根指南保存。

新环境自动化部署方案已形成设计文档，建议首批 Windows 11 x64 / Windows PowerShell 5.1，分别验收 Node 合成评测、ADB 工具准备和经本轮确认后的手机环境检测；Python venv、模型认证、Android 与开发编排工具不作为当前评测必需。Android 检测不等待完整业务，但须先实现检查器与共享设备门禁；部署入口、引导发行物与检查器尚未实现，干净机器及手机验收均待执行，本轮仅文档、未运行 ADB；既有本机测试记录不能替代该验收。

项目特有约束与已知限制：

- 默认使用标准库，仅在真实需要时添加依赖；评测依赖按 npm 开发依赖管理。M1/M2 使用 Python 标准库 `sqlite3`、`xml.etree.ElementTree`，没有第三方 Python 依赖。源代码顶部说明用途，函数和方法写 docstring/JSDoc；修改模块时同步目录指南。
- 源码、环境与数据库只依赖当前独立项目，不依赖原项目虚拟环境、数据库或仓库外业务规则母本；当前不进行旧个人策略通用化，不恢复已清理业务代码与 Skills。
- 不扩展真实自动发送授权，不继承旧工作地点、简历事实或联系方式授权。黄金集中的城市、薪资、记忆、授权与 HR 消息均为合成输入；can_send=true 只是拟执行测试决策，不触发真实发送、记忆写入或手机动作。
- 数据库、日志、契约、截图、凭据和个人事实只在本机保存。不读取或记录密钥、令牌、密码等敏感值；不读取根 config.json 或私人事实来填充测试集。新增故障案例须先脱敏并确定独立判据，不直接把模型输出当正确答案。
- 操作手机前说明具体动作、预计占用时长和是否独占，并取得本轮确认；未确认不得连接设备或运行 ADB。npm 自检和黄金集评测均不操作设备。
- 多 agent 共用同一台手机只靠 M4c 固定 OS 共享门禁互斥：`LOCALAPPDATA/job-application-openagent/device-guard.sqlite3` 的唯一 `primary` 行跨进程、跨 worktree/clone、跨 profile 生效，先占后用；冲突立即返回 `device_busy`/`device_quarantined`，不排队、不重试、不因 TTL/PID/重启/换 profile 自动释放。每个 agent 用自己进程的 owner/pid/host/nonce 取占用，只在自己明确收敛时释放，不共用 owner、不代持 nonce，未持有时不启动任何 ADB 命令（含裸 `adb`）；`--confirm-device` 是每个 agent 每轮各自的确认，一次授权不转授给其他 agent。占用期间其他 agent 只读或等待：同 profile 的 `facts set/revoke/forget --confirm` 已被门禁拒绝（`device_busy`/`device_quarantined`），未确认提案与 `show` 仍可用，其他 profile 不受影响；`prompts/local` 手工编辑仍不经门禁，只有 `policy restore` 会因 held→active 被拒绝。门禁卡住（崩溃留下 held/quarantined）时用只读 `python -m app device gate inspect` 查看状态、profile、占用 PID 与时间，但不得删库、改 owner、换路径或伪造开锁，也没有清除/强制解锁命令，只能报告使用者并等待对账入口。门禁只覆盖单台逻辑 `primary` 设备，多手机并行不在设计内。
- config.example.json 当前只含 `policy_pack`，指向被忽略的本机策略包；其余业务 schema、字段和默认值仍待设计。M0 最小配置规则已在 CLI 实现（只读模式两项键），不写入本示例文件。config.json 是被忽略的本机文件，不作为示例或授权来源，不沿用旧批次大小、每日额度或 HR 间隔。
- M2 `sense` 只接受合成 manifest/profile/XML，profile 是离线开发契约，不代表真实 BOSS 版本已标定；不得读取 expected.json、不得连接设备/模型/网络、不得 claim/start/finish、不得打开会话或推进已读基线。manifest 引用必须留在夹具目录内，拒绝绝对路径、`..`、链接与越界；未知字段不当零，过期/未来/未标定/非法 XML 不落库。
- M3 `read` 只接受合成 M3 manifest/profile/XML 与显式合成像素标定，不连接设备、业务库、模型或网络，不 claim/start/finish、不关闭发现、不写正文或 verified 基线。页面/职位/未读/会话值必须严格校验；会话正文/职位正文只在内存与合成 CLI 数据区，不写日志/事件/库。稳定消息身份只能来自 profile 验证过的应用 `content-desc`，内容指纹/同秒/坐标/屏幕序号/布局 resource-id 不能生成可发送入站键；未标定真实版本保持 unknown。真实采集适配器只在测试 mock subprocess；真实 read 在共享 OS 设备门禁交付前任何子进程启动前拒绝，profile 内 primary 任务锁不能替代。真机像素/字体/分辨率/稳定标识与只读一轮未测。
- M4a 只交付 Envelope/Decision 严格契约、TOML+三 md 策略包、45 组合与硬下限、四类无自引用摘要、system/developer/user 纯渲染、披露复验和 `policy init/validate/preview/list/snapshot/restore` 文件流程；不接事实确认、设备或模型。策略命令成功/失败使用四键 `errors` 外层，为既有三键的明确扩展；`base_hash` 使用 `prompts/base/<文件名>` 项目相对 POSIX 路径，preview 固定读取 `tests/fixtures/policy-preview/`（公开运行资源，部署须携带）。M4c 已把生产 `restore` 接真实 profile/共享占用检查：held 为 active，quarantined/坏库/绑定不符为 unknown，M1 leased/running 为 active、blocked 为 unknown，仅 stopped 允许恢复；失败不换目录解锁或重置额度，不再固定拒绝。preview/validate/snapshot 通过不证明模型语义安全或发送授权，v1 无独立 A 档前置判据时固定 C。
- M4b 只交付 A/B 本机子进程决策桥、单次决策桥前/重试前/桥后复验、`policy eval` 实包分层评测与 `policy holdout register/retire`；不接真实 provider、M4c 真实活动/事实确认、M5 授权/动作或设备。桥只用 argv 数组、`shell=False`、`stderr=DEVNULL`、20 秒总预算与本机临时文件承载 stdin/stdout，stdout 只读 `64KiB+1` 哨兵，超限/超时 kill/回收且不残留读取线程；固定 system/developer/user 三消息与独立回显，不接收输入指定 role/tool/message。角色/工具/历史声明只是协议声明，不证明第三方实际能力。只有传输层可证明进程未启动时重试一次；重试前复验时钟/租约/包，提交后超时/schema/回显/证据失败丢弃且不重发。`policy eval` 必须实际加载当前包并经同一桥路径，公开套件与旧 24 npm 回归独立；映射只登记责任，旧 expected/reference 不得回填新 Decision。报告/凭证只写 `data/evals`，失败/零样本/缺私有套件/非公开集/simulated 不生成有效验收凭证且零样本 authorization 未确认；`--confirm-external-send` 只确认本轮模型外发与计费，不授予设备/事实/业务发送权限，也不替代留出许可。留出按语义输入指纹（排除 case/task/nonce/now 等运行字段）占用历史样本，已看样本不得换批次/候选/套件或拆子集重冻结；访问意向在调用前锁内持久化，完成/失败/未知/重复如实区分，退役整批写实际 regression 准入副本并可被同一生产 regression 路径运行；无上传合同必须绑定当前 model_id/model_config_hash 且所有禁用项为真。真实 provider 与私有留出明确未测。
- M4c 只交付 profile 独立事实库/本机事实入口、固定 OS 共享 SQLite 设备门禁、只读 `device gate inspect` 占用查询、事实确认写入的 gate→profile 原子写保护、restore 真实活动检查、验收凭证复核与 run check 预检；不执行 M5 动作、不调用真实 provider/设备/网络。profile 库固定 `data/runtime/profiles/<profile_id>/runtime.sqlite3`，复用 M1 v1 五表原文并追加最小 `profile_meta/profile_facts/profile_fact_events`，不虚构 1→2 迁移，旧合成库不自动导入；合法 profile 库在完整 profile 校验后由唯一 `SqliteStore` 复用任务能力，旧普通 M1 库验证不放宽，真实 `ProfileSessionLease` 每次重核任务/设备租约与共享门禁，失效/失配/过期不返回租约。事实只注册三键，值只在本机 CLI/库/请求内存，程序生成来源 ID；有效期/冲突/同值重复由唯一 `select_allowed_facts_with_reasons` 产出 allowed/原因，profile 侧只组织无值待办，`forget` 只保留无值来源审计且不宣称擦除 WAL/既有备份；带 `--pack` 时 pack.profile_id 必须等于 `--profile`；同 profile 设备会话 held/quarantined 时 `set/revoke/forget --confirm` 固定返回 `device_busy`/`device_quarantined` 且不写库，未确认提案与 `show` 不受影响，门禁库不可读失败关闭。确认事实写在固定门禁库以 `BEGIN IMMEDIATE` 写事务覆盖最终同 profile 校验、profile 打开/迁移与事实提交，统一 gate→profile 锁序；状态先于 profile 比较验证，不可识别状态对任何 profile 都失败关闭；允许不同 profile 逻辑通过，但整个写区间保持原子，不插入或改写占用行，异常回滚、进程死亡由 SQLite 释放事务锁；首次 Windows 确认写可初始化严格校验的空门禁库。共享门禁固定 Windows `LOCALAPPDATA/job-application-openagent/device-guard.sqlite3`，唯一 primary、全会话先占先用、路径不可自选；acquire 保存完整 owner/pid/host/nonce/profile/路径并以显式零等待立即把 `SQLITE_BUSY` 映射 `device_busy`，verify/release/recover/session_identity 写事务共用完整身份复核并有界等待事实短事务，busy 时返回 `device_busy`，只读 inspect busy 返回保守 `device_gate_io_error`，内部 busy 分类不越过端口且不释放/改写占用；任一变化不删除原占用；只允许明确新空库初始化，未知版本/额外对象/旧占用表不改写；目录/不可读等存在但不可判定路径返回 unknown，只有确认不存在才是无状态正例。崩溃/PID/TTL/重启/换 profile/nonce 篡改/库不可读都不自动解锁；门禁适配器唯一记录 acquire/verify/release/quarantine/recover/facts_guard 的 started/终态，只读 `device gate inspect` 由 CLI 记录同一 `device_gate` 阶段，run/restore 的 profile 打开记录 started/终态。M0 真实探针与 M3 采集适配器缺 gate 时在零子进程处拒绝，`doctor --device` 需 `--confirm-device` 且每条 ADB 命令前复核。`policy acceptance check` 只读比较凭证与当前 base/pack/provider/model 摘要，变化即 `policy_changed`；`run check` 只预检 read_only 本轮确认、draft 当前验收与 A 档固定拒绝，不探测设备、不调用模型、不执行动作、不预建 M5 表，预检通过不构成发送授权或完整 MVP 验收。实际 CLI 合成桥 eval→snapshot→restore→validate/preview→eval 串行闭环已运行，三摘要一致且同 profile 事实/任务审计不变。真实 provider、私有留出、真机与完整运行未测。
- M5 只交付五类不可逆动作护栏、profile 结构 v2 动作账本、持久化文本额度/冷却、固定人工卡与受控对账；不执行真实 ADB/设备动作、不调用真实模型/provider，不启用真实 A 档。动作键精确为文本 `app:message_key:reply`（本仓库 message_key 已含 `boss:msg:`）与其余 `conversation:action`；必须先在同一仓储事务写 pending 与唯一动作键锁、再调用一次执行器，`performed` 只由不同前后快照与动作特有变化证明，点击后异常/断连为 unknown，绝不自动重试。动作结果、任务状态、设备处置与 `action_events`/M1 `events` 一次提交；unknown/未收敛 pending 保留动作锁并隔离设备，abandon 只关任务且不解锁。文本日额度按 profile 的 UTC 日期跨 app 合计 performed+sent 文本，冷却按 profile/app/会话，pending/unknown 先阻断；额度检查与 pending 同事务，换包/restore/重启/调低额度不清零。分动作授权按任务/动作键/base/pack/验收摘要/期限/确认/nonce 绑定，文本授权不覆盖附件/联系方式/删除；固定 C 与合成资格正例独立，`eligibility` 为真不授予真实发送。profile v1→v2 迁移先标准库 SQLite backup 再单事务追加，普通 M1 库/额外对象/未知版本不宽放。CLI 只接合成动作脚本；真实 ADB 原语仅 mock subprocess 验证参数数组/5 秒超时/每命令门禁复核/不重试。真机动作、真实恢复时序、M6 调度与完整 MVP 未测。
- M5 第2轮返修后：默认动态时钟与验收四摘要纳入每命令/提交前 guard；执行后失效按原 owner/nonce 冻结 unknown/blocked 并保留隔离；当前租约核验唯一化并支持合法真实续租后的旧捕获 Lease；动作前置锁定唯一姓名+职位、唯一 editor、非滚动完整列表、可点未处理槽位与目标子树差分；完成 IME 绑定持久原值，切换后早退有界恢复；共享门禁 recover 同 host，生产命令检查器检查父子进程；日志白名单/提交配对/原始 XML+UUID 证据索引补齐；重复 Evidence/租约/pause 实现收口。真实设备/模型仍按未测处理。
- M5 第4轮返修后：`perform_action` 必须携带非空完整身份标签并贯通到 `ActionPlan`，缺失在 validate 阶段 `precondition_failed` 且不可逆点击为 0；CLI `action perform --identity-label` 暴露该身份。联系方式效果只统计本动作类型槽位，确认阶段重核 dialog 同类型槽位；删除覆盖按目标/锚点祖先链显式非滚动容器分别核对 before/dialog/after。`CommandIdentityStore` 按实际执行回调记录命令族后代，缺失/坏行/枚举失败/写入失败都保持 unknown，`on_pid` 失败与超时 kill/回收均有预算。`action reconcile --device --serial` 复用 `AdbRecoveryObserver` 读取实际现场/IME，坏 XML/错 IME 拒绝；采集保存与 guard 拒绝记 started 与单一终态。真实设备/模型仍按未测处理。
- M5 第5轮返修后：公开动作身份改为结构化 `ActionIdentity`（姓名/公司/职位），重算会话键必须与持久任务一致，缩减/冲突在 pending 与设备调用前拒绝；CLI 改为必填 `--contact/--company/--position`。联系方式由唯一 `_slot_node` 定位，嵌套容器/按钮/状态只算一个槽位，另一类型已处理不污染本类型；列表覆盖只认可真正列表容器的显式非滚动与 bounds 覆盖，行/按钮自身 false 不算。命令账本改为 begin→PID/后代→complete 分段，公开入口在首次 IME 前绑定并覆盖共享采集，结束再展开记录晚生后代；部分写/枚举失败/未闭合保持 unknown。真实设备/模型仍按未测处理。
- M5 第6轮返修后：列表覆盖要求容器内全部可见行/后代在容器内且容器未被外层可见区域裁剪，缺失/坏/零面积/自相矛盾 bounds 不默认成功，复用 `boss/support.py` 几何 helper；同分组两个独立同类型槽位保留歧义并零点击，效果定位不要求按钮动作后仍 enabled，本类型精确新状态仍证明 `request_sent`。命令族使用 Windows Job Object 做有限有界终止/核验，无平台有界或未证明族停止时保持 unknown 与隔离；`store.task_conversation` 读取故障在 validate 阶段补安全单一终态。真实设备/模型仍按未测处理。
- M5 第7轮返修后：`_list_complete` 对容器内所有有可见意义后代强制可解析正面积 bounds，并逐层核验全部祖先 bounds 与列表滚动事实，不再遇首个合法祖先即 break。命令族在 Popen 前先建 Job、以 `CREATE_SUSPENDED` 启动并在加入后恢复，Windows 权限改为 `PROCESS_SET_QUOTA|PROCESS_TERMINATE`；mock 进程不触真实 Job，平台不可用/未证明/持久化失败保持 unknown。执行器任一命令未证明全停时公开观察 `command_quiet=false`，保留已证效果但记录 unknown、不报 `gate_converged`、保留共享隔离并生成人工卡；本机自有合成 Python 子孙真实 Job 停止正例单独验证。真实设备/模型仍按未测处理。
- M5 第8轮返修后：列表覆盖把 `content-desc` 计入可见意义，并按每个有意义节点的真实祖先链逐层核验中间/外层 bounds 与 scrollable 事实。命令边界在 Popen 前先写账本未闭合事实，assign 失败不 resume/不执行，assign/resume/首次 PID/二次超时/wait 再超时/OSError 统一有界 finally 并 terminate/close 专属 Job；Job 使用 kill-on-close。执行器任一命令未证明全停、`finish_family` 写失败或空 IME 早退都保持 `command_quiet=false`，动作记录 unknown、任务暂停、`gate_converged=false`、人工卡与 profile/共享隔离保留，已证效果不改写。真实设备/模型仍按未测处理。
- M5 第9轮返修后：唯一账本闭合步骤在真实 complete 写失败时由同一 ProcessLog 记录 `stage=action/operation=action_finish_command` 的 started→failed 固定安全终态（`store_io_error`/`store`），可关联本次运行且不写异常原文/参数/现场/绝对路径；正常与可靠全停路径记录 started→ok，不产生虚假 failed。公开仍保持 unknown、`gate_converged=false`、任务暂停、profile/共享隔离与人工卡，已证效果保留。真实设备/模型仍按未测处理。
- M5 第10轮返修后：真实 `AdbActionExecutor.execute` 要求已 `bind_action` 且非空 `CommandIdentityStore`，缺失/未绑定/错绑定在任何 ADB 子进程、IME 切换或点击前固定 `executor_interrupted` 拒绝；动作上下文绑定后的 `read_current_ime` 预读同样要求账本，未绑定动作的独立读取只服务 reconcile。生产 `perform_action` + 无账本真实适配器（含 IME 预读路径）只留 pending/blocked、`gate_converged=false` 与隔离，不可能 performed/done。证据索引写失败传真实 clicked 与 phase 的现场 flags，before/readback/dialog 不固定 clicked=True；readback 已输入，不按 idle 推断输入为空/IME 已恢复，输入/回读异常保留底层未证明命令族，不硬写 scene_settled/command_quiet；点击后仍 unknown 且不重试。v1→v2 备份前核验完整 DDL 与 profile 身份，错 profile/普通 M1/坏库零备份，既有备份不删除/覆盖。会写业务终态的 `action reconcile` 固定只允许 draft_only，read_only 在打开/迁移 profile、取得/恢复门禁、读取设备或写状态前拒绝。`run check` limitations 改为真实设备动作入口/M6 调度/M8 run 未交付与设备探针未运行。真实设备动作组合根（L2）仍未交付，ScriptedActionExecutor 不能冒充。真实设备/模型仍按未测处理。
- promptfooconfig.json 的默认 provider 为 echo 占位，sharing=false、并发上限 1；通过 `npm run eval:smoke` 才回放参考 provider。`npm run eval:model -- "<provider-id>"` 必须指定 provider，拒绝 echo 和内置参考 provider；真实评测将合成输入发往所选服务，认证与计费按该 provider 配置。
- evals/run.cjs 设置 PROMPTFOO_DISABLE_TELEMETRY=1、PROMPTFOO_DISABLE_UPDATE=1，配置 PROMPTFOO_CONFIG_DIR 为本项目 data/evals/promptfoo、PROMPTFOO_CACHE_PATH 为 data/evals/cache；通过 --no-share、--no-cache、--no-write 关闭分享、缓存和评测数据库保存。逐次 results.json 写入 data/evals/smoke-UUID 或 model-UUID。直接使用裸 promptfoo 命令不会自动继承本入口的环境设置。M4b `policy eval` 是另一条 Python 入口，不调用 promptfoo、echo 或参考回放；其报告/凭证/留出登记同样只写 data/evals，且 simulated transport 永不生成验收凭证。
- 黄金集当前只评测给定记忆的单次决策，未验证 SQLite 持久化、跨会话检索或动作执行。自由文本只做关键词约束，不证明全部语义正确；smoke 的参考回放通过率不代表真实模型能力。
- .gitignore 排除 .venv、node_modules、.promptfoo、Python 缓存/构建产物、本机配置、凭据、数据库/日志/锁和设备证据；data 与 screenshots 仅为指导文档保留提交例外。opencode.json、环境文件和旧私有事实路径只是忽略约定，不表示这些文件存在或服务已配置。
- Skills 后续变更须保持 skills/、.agents/skills/、.claude/skills/ 语义一致，当前均不存在，按规则不创建空目录。
- 全局规则原文唯一母本为使用者提供的《项目初始化提示词.md》规则区。母本位于仓库外，实际位置由使用者在本机提供，公开文档不记录其绝对路径。改规则须同步母本和所有已生成项目的对应内容；该文档同步关系不构成业务运行时依赖。

### 开源项目敏感信息告知

本项目面向公开开源。开发、排查、编写文档、构造测试或准备提交时，发现实际或疑似敏感信息，必须主动在当前对话中告知使用者，不能只依赖忽略规则或静默跳过。

- 告知范围包括密钥、令牌、密码、Cookie、认证请求头、带凭据的 URL；真实姓名、联系方式、简历、身份信息、薪资和个人偏好；HR/公司与聊天内容的关联；数据库、契约、日志、截图、UI 层级及评测报告中的真实业务数据。
- 本机用户名、绝对路径、内网地址、私有服务地址、设备序列号和私有仓库信息也按可能暴露环境或身份的信息处理。公开示例优先使用相对路径、占位符和合成值，不把测试中的合成事实当作使用者事实。
- 通知只说明文件位置（必要时行号）、信息类别、可能的暴露途径和建议处理方式，不在对话、差异摘要、提交说明或报告中复述敏感原值。疑似凭据无需读取完整值即可通知，也不为检查而主动打开无关的本机私密配置。
- 能在当前任务范围内安全处理时，使用脱敏或合成内容替换公开材料，将真实数据留在被忽略的本机文件，并告知处理结果；不能确定公开范围或替换含义时，先告知并请使用者明确。只暂停涉及该信息的提交或外发步骤，其他独立工作可继续。
- 提交、推送、发布、上传附件或调用外部模型/服务前，核对实际待提交或待发送的内容；`.gitignore` 不代表已跟踪文件或 Git 历史已脱敏。未处理的凭据不进入仓库或外部请求；未经明确公开授权的个人或真实业务信息不公开，评测继续只用合成数据。
- 若发现敏感信息已经进入提交、历史或外部服务，立即告知暴露位置与已知范围；不得声称删除工作区文件就已消除历史暴露，也不擅自重写 Git 历史或操作账户凭据。需要使用者处理的撤销、更换或历史清理事项应明确列出。

## 二、根目录索引

- `README.md`：当前范围、M0 只读探针、M1 状态库、M2 合成传感器、M3 离线读取、M4a 策略包离线命令、M4b 桥/eval、M4c profile 事实/共享门禁/验收复核/run check、M5 动作护栏、M6 调度/安全点/预算/合成 run、业务和评测环境、Python/npm 命令、数据边界与未实现部分；可独立阅读的使用概览，不是全局规则母本。
- `LICENSE`：MIT 许可证，2026 年项目贡献者署名。
- `app/`：M0–M6 分层程序包。`__main__.py` 提供 `python -m app`；`cli/` 负责严格参数、配置、路径与 JSON 输出，接线 `doctor`、`store init`、`sense`、`read`、`policy`（含 `eval`/`holdout`/`acceptance`）、`facts`、只读 `device gate inspect`、`run check|prepare|execute` 与 `action`（含 `perform`/`eligibility`/`card`/`reconcile`）；`domain/` 提供纯领域模型、H/时间/键算法、严格校验、共享严格 JSON、M2 聚合/停因、M3 读取值类型/字段阶梯、M4a 决策/策略包/摘要/渲染、M4b 套件/断言/留出校验、M4c 事实状态筛选、M5 动作键/观察→三态证据/授权/人工卡与 M6 预算/安全点/候选优先级/身份游标/抢占审计，不导入适配器；`ports/` 声明设备探针、`ScreenPort`/共享设备门禁、`StorePort`、M4a 策略文件/恢复活动状态、M4b 决策桥/当前包快照/可信租约/评测文件、M4c profile 库/共享门禁、M5 设备动作与 M6 场景/决策/批次端口；`usecases/` 做输入校验、唯一 doctor 结果组装、sense 持久化/聚合编排、read 门禁/定向核对/JSON、policy 文件事务、单次决策编排、policy eval/留出编排、facts 确认编排、验收复核、run check 预检、M5 动作/资格/对账与 M6 `prepare`/有界 `execute` 编排与操作手册隔离报告控制；`adapters/adb/` 实现仅显式触发、只接受 tab 分隔确定清单记录的真实 ADB 探针及 M3 层级/截图采集、M5 动作原语/恢复现场观察，并在每条子命令前复核共享门禁；`adapters/boss/` 实现 M2 合成 profile/manifest、XML 四源解析/分页合并与 M3 页面/职位/未读/会话、像素和跨屏合并；`adapters/files/` 实现 M4a 项目内策略读取/暂存/同卷发布/受控清理与 M4b 套件/报告/登记；M4c 决策快照复用唯一 `RepositorySnapshot`，不另建文件包装；`adapters/llm/` 实现 A/B 参数数组子进程桥、固定三角色与固定诊断；`adapters/sqlite/` 实现 v1 五表/约束/索引、初始化核验、BEGIN IMMEDIATE 事务仓储与 primary 设备租约，以及 M4c profile 库/事实存储/固定 OS 共享门禁/restore 活动检查/真实 profile 会话租约、M5 profile v2 动作表/额度/对账与 M6 profile v3 职位/批次/草稿/决策记忆/调度事件及类型化 `apply_job` 动作账本；`adapters/simulated/` 提供 M5 脚本化动作执行器与 M6 合成场景/决策刺激；`process_log.py` 追加固定字段的本机过程日志（含 `store/sense/read/policy/facts/profile/device_gate/action/run/eval/decision/adb_hierarchy/adb_screen` 阶段）。M7 清理与 M8 灰度模块尚未创建。
- `tests/`：M0–M6 标准库 `unittest` 与纯合成夹具。`python -m unittest tests.test_doctor` 覆盖正反例、畸形 keyguard、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单行多组件/前后附加内容、重复 JSON 键、孤立代理字符、提示行、`output` 成功/失败、日志白名单/故障注入、ADB `started→结束`、`--confirm-device`/共享门禁与 fixture 零子进程断言；`tests.test_store` 覆盖合成快照入库、竞争、TTL、停止未知/异主机、三态收敛、blocked 设备隔离、旧 nonce/截止时间、clock_changed 与三类事务故障注入；`tests.test_migrations` 覆盖建库/幂等/回滚、schema 篡改、lease CHECK/FK 事务回滚/RESTRICT/active 唯一冲突、同事务先成功后 FK 失败回滚/重开与 `store init` CLI；`tests.test_sensor` 覆盖 M2 XML/profile/manifest、四源聚合、分页合并、过期/未标定、独立键算法与 stop_reason；`tests.test_sensor_store` 覆盖 sense 落库/重放/终态/仓储错误/日志关联/零设备网络进程；`tests.test_read` 覆盖 M3 页面/职位/未读/会话、字段阶梯、像素阈值/分辨率/损坏、稳定消息身份、跨屏合并、门禁与五类 CLI fixture；`tests.test_screen` 覆盖 mock ADB 参数/超时/不重试/证据保存、每条子命令前共享门禁复核与 read 门禁；`tests.test_policy_contract` 覆盖 Envelope/Decision、摘要、限额与回复复验；`tests.test_policy_pack` 覆盖 TOML/三 md、45 组合、硬下限、渲染与 preview schema；`tests.test_policy_store` 覆盖 policy 六命令、路径/大小/编码/链接、快照/恢复三值、坏 profile 库拒绝、四键外层与日志，以及 Windows 短暂/持续目录句柄下的有界 rename 重试、暂存所有权/替换/跨实例/内容摘要拒绝；`tests.test_bridge` 覆盖 A/B 真实参数数组子进程、三角色、一次启动重试、提交后超时与认证/额度/断连/角色/协议诊断；`tests.test_policy_eval` 覆盖实包加载、报告绑定、分桶、24 行映射、成功/失败凭证与留出冻结/访问/退役/零样本；`tests.test_profile_facts` 覆盖事实提案/确认/再显示、来源/有效期/冲突/撤销/遗忘/无值审计、包白名单待办与 pack/profile 身份一致、设备会话占用期间确认写入 `device_busy`/`device_quarantined`、提案与 `show` 不受影响、gate→profile 原子写保护与跨进程竞态、日志无值/profile 打开配对与权限错误、旧库不导入；`tests.test_device_gate` 覆盖固定路径、跨进程/跨项目根互斥、完整 owner/profile 篡改、未知版本/额外对象不改写、崩溃/PID/TTL 不释放、nonce 篡改、库不可读、隔离、只读 `device gate inspect`、profile 活动检查与真实 `recover` 正反例；`tests.test_run_guard` 覆盖 read_only 本轮确认、draft 验收、A 档拒绝、跨库故障隔离与门禁/profile 日志配对；`tests.test_profile_session_lease` 覆盖合法 profile 库接入唯一 SqliteStore、领取/续租/失效、跨门禁 profile/原库/owner、复制库且参数指向副本、任务两行 owner 同改、门禁 nonce 桥前/桥中变化、错误任务 key/app、其他 profile 快照拒绝与 decide 桥前后拒绝；`tests.test_m4c_closed_loop` 覆盖真实活动恢复、目录/权限路径不发布、三值一致、事实/任务审计不重置、旧凭证失效、唯一仓储快照重核与实际 CLI 合成桥 eval→snapshot→restore→validate/preview→eval 串行闭环；`tests.test_action_domain` 覆盖动作键/观察→三态/授权/人工卡；`tests.test_action_store` 覆盖 pending 先落库、额度冷却、事务回滚、v1→v2 备份前身份/DDL 核验与错 profile/坏库零备份、unknown/pending/abandon；`tests.test_action_usecase` 覆盖授权精确、无账本（含 IME 预读）零子进程、真实合成子进程+真实账本 performed/done、证据索引 before/readback/after 失败三态与任务/设备处置、unknown 单次调用、零重放与当前证据；`tests.test_action_executor` 覆盖 ADB 原语参数数组/超时/不重试、无账本/未绑定/错绑定与绑定后 IME 预读零命令拒绝、证据索引 clicked/phase 收敛（readback 保守输入/IME/命令状态）与合成执行器、apply 原始 XML 控件发现/无控件零 tap；`tests.test_action_cli` 覆盖五动作正反例、资格、对账、read_only reconcile 零业务/门禁写入、abandon 隔离与 CLI 闭环；`tests.test_runtime_domain` 覆盖安全点七条件、预算等号/超支、游标身份恢复、优先级/一次核对/第 6 次抢占/单调等待/双时钟；`tests.test_runtime_store` 覆盖检查点故障回滚、`apply_job` 三态/unknown 隔离、typed 任务引用与 v2→v3 迁移；`tests.test_runtime_execute` 覆盖 M3/decide 实际组合、兼容候选/cleanup 不成为权威、read_only 实际计数与零业务写、安全点独立证据、实际 unknown 未读暂停恢复、实际读取预算/负超支/游标、缓存证据身份/一致损坏/事实撤销、needs_human/非法决策不标 done、completed/queued 批次 restore 边界、缺本轮确认零外部工作与 apply 配额失败关闭；`tests.test_runtime_apply` 覆盖 perform_action+真实临时命令账本/执行器的 apply performed/applied、真实 `AdbActionExecutor` 原始 XML/合成 adb 子进程控件发现、pre-command/无控件拒绝、after-click unknown、本地 complete 失败 pending 不重放；`tests.test_runtime_g123` 覆盖 G1 唯一安全点派生（空 checkpoint、检查点事件故障、A/B 动作不互相洗白/阻断、空/非法身份与尚未 start 的区分、历史已收敛不阻断）、G2 实际一次定向 M3 采集/预算/耗时/日志与普通读取先结算、hint/absent/viewport/count/过期/未来负控、G3 首次观察取真实采集完成时刻（含定向首次带来 present 未读）、S01 20200 开始与安全点前零切换、S08 三窗（含原始可见性切换）、未 start 零事件、漂移/预算阻断；S05 两卡游标重排不重复；S06 已交付唯一 AdbRuntimeRead 的 run_adb.on_spawn 实际创建计数与真实 execute_run 199/200/201/250 矩阵；长 read 逐命令续租仍待补；S07 1001ms finish、20/21 秒最新 Lease renew+finish、25 秒 bridge 后 done、start 后 nonce 变化零命令；`tests.test_operator_docs` 覆盖手册存在与 `operator_docs_report` 缺失/不完整时 BLOCKED；`tests/m1_helpers.py` 提供假时钟与合成快照/查询辅助；`tests/policy_helpers.py` 提供临时项目根（含公开套件）与合成 base/包/决策辅助；`tests/action_helpers.py` 提供 M5 临时项目根、验收凭证、seed/动作/对账脚本与 CLI 辅助；`tests/runtime_helpers.py` 提供 M6 临时项目/profile/场景/执行辅助；`tests/fixtures/bridge/bridge_stub.py` 是合成本机桥桩，`tests/fixtures/actions/` 是纯合成动作观察脚本。独立场景协调端维护的 `tests/scenarios/`、`tests/test_offline_scenarios.py` 与 `tests/private_regression/` 不在本 Task 范围，M6 调度判据不由其替代。
- `pyproject.toml`：正式名称、版本 0.1.0、Python >=3.13、MIT 与空 Python 依赖；M0–M6 入口在 `app/`，尚未声明发行包。
- `requirements.txt`：Python 标准库及无第三方依赖声明，不代表旧业务测试仍存在。
- `package.json`：评测工具元数据、Node 版本要求、固定 promptfoo 开发依赖和 npm test/eval:smoke/eval:model 入口。
- `package-lock.json`：npm 依赖完整锁文件，使用 npm ci 重现安装；依赖更新时同步维护。
- `promptfooconfig.json`：根评测配置，引用 evals 提示词、案例和断言；echo 为占位，不当作真实模型，默认关闭分享、并发 1。
- `evals/`：24 个合成黄金案例、单次决策提示词、断言、正反例自检、参考 provider 及 smoke/model 运行入口；另有 M4b `policy-suite/` 公开合成套件与旧 24 行责任映射，独立于 promptfoo 回归。
- `docs/`：公开文档目录。终稿 `BOSS直聘_手机客户端原始操作_原子化分析.md` 合并旧项目 `scripts/` 的静态阅读结果，按 CORE/JOBS/HRR/HRA 前缀的原子 ID 组织，含 0–9 小节与源码索引；技术结论带 `file:line` 证据，不构成设备执行授权。同目录另有 `BOSS直聘_实机验证偏差与修订建议_20261123.md`（单机型单版本，记录 7 条偏差与 5 条待确认）、`BOSS直聘_求职自动化_简化架构设计说明_20261123.md`（失败证据、简化架构、接口与验收指标）、`BOSS直聘_求职自动化_实施计划_20261123.md`（M0–M8 里程碑、六张图、测试与回滚）、`求职自动化_多应用扩展与分层_设计说明_20261123.md`（分层、端口、多应用共存与接入猎聘步骤）、`求职自动化_公开仓库与策略包_设计说明_20261123.md`（公开/私有资产边界、策略包与黄金集分层）、`求职自动化_新环境自动化部署方案_20261123.md`（新环境现状证据、依赖矩阵、拟议引导与部署入口、权限/恢复/配置保留、ADB 工具准备、手机检查及 A01–A08 独立验收、官方来源与干净机器验收矩阵；尚未实现安装器/检查器或验证初始环境/手机）、本目录 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`。
- `docs/BOSS直聘_求职自动化_场景测试方案_20261123.md`：M0–M8 场景规程与随机未读分支，区分现有自检、未来离线、受控设备与真实观察/写入；含独立判据、证据、指标和发布补测，只做方案不操作设备。
- `docs/BOSS直聘_场景测试方案_三轮自审记录_20261123.md`：场景方案三轮作者文档自审的发现、修订、复核与阶段提交证据，不代表实机或独立测试人员验收。
- `docs/BOSS直聘_M3读取阶梯与最小数据契约_20261124.md`：M3 离线读取阶梯与最小数据合同。定稿 `ReadResult` 值类型分派、页面/职位/未读/会话值类型、字段证据与决策门禁、跨屏稳定消息身份（内容指纹与稳定身份分离）、会话身份核对、像素标定与失败边界、`read` fixture/CLI 形状、采集适配器与场景责任，以及 §10.1 的 M2/M3 定向核对类型分派（M3 真实目标/全覆盖 absent 才解除，viewport/count-only 仍未知）；不改 M1 五表、不持久化正文，真实 run 在共享设备门禁前拒绝，不代表真机标定或只读一轮通过。
- `docs/BOSS直聘_M4c运行绑定与事实设备门禁_20261124.md`：记录 M4c 已实现的 profile 独立事实库/本机事实入口、固定 OS 共享 SQLite 门禁、restore 真实活动检查、验收凭证复核与 `run check` 预检的设计选择、数据边界、测试范围与未测项；不构成设备、模型、发送或 M5 动作授权。
- `docs/BOSS直聘_M5动作护栏与对账_20261124.md`：记录 M5 五动作键/前置/效果、profile v1→v2 备份迁移、分动作授权、唯一文本额度/冷却、观察→三态证据、人工卡与受控恢复、`action` CLI、第1–10轮审核返修（含执行账本边界、证据索引点击事实、迁移前身份/DDL 核验、reconcile 仅 draft_only 与 run check limitations）、测试范围与真实动作未测项；不构成真实发送或 A 档授权。
- `docs/BOSS直聘_M6调度与运行_20261125.md`：记录 M6 B1–B8 返修、b4 固定返修、ffc 最终独审余留与 §九 G1/G2/G3 生产边界：M3/decide 生产组合、实际候选/缓存证据身份与一致损坏拒绝、语义阻塞、暂停恢复、七条件独立安全点、唯一 `derive_safe_point` 真实任务身份/动作来源、实际一次定向 M3 采集与预算耗时、`on_boundary`/`on_started` 到达与开始分离、实际单调时钟/设备计数与负超支、`apply_job` 原始 XML 控件发现/before-done 前置/E2E、M2 queued new_message 第二级、批次 completed/queued restore 边界、操作手册隔离报告控制、失败边界与未测项；另列策略发布 Windows 短暂句柄的有界 rename 重试与暂存所有权/内容身份复核、普通 M1 job-key `invalid_input`。不构成真实设备/模型/apply 配额合同或完整 MVP 验收。
- `docs/operator.md`：M0–M6 操作手册。说明 fixture 命令、最小配置、显式真实设备模式与 `--confirm-device`、设备确认要求、JSON 字段、退出码、夹具规则、`store init` 路径/幂等/schema/租约/错误码、`sense` 的 manifest/profile/XML/四源/分页/停因/落库规则、`read` 的页面/职位/未读/会话/字段阶梯/像素/跨屏身份/门禁、`policy` 的初始化/校验/预览/列表/快照/真实活动恢复/四键错误、M4b `policy eval` 的 provider/授权/桥超时/报告/凭证/留出生命周期与 `policy holdout`、M4c `facts` 确认/撤销/遗忘、共享设备门禁与只读占用查询、gate→profile 原子写保护、`policy acceptance check`、`run check` 预检、M5 `action` 的动作键/授权/额度/证据/人工卡/受控恢复、M6 `run prepare|execute` 的唯一安全点派生/真实一次定向采集/`on_boundary`/`on_started` 开始边界/预算/游标/read_only/草稿/决策记忆/apply_job 边界、命令账本边界、reconcile 仅 `draft_only`、迁移前核验、`run check` limitations、本机过程日志路径/字段/复核方法与未实现范围。
- `prompts/`：M4a 公开 base、纯合成示例与 `policy init` 生成的本机策略包位置。`base/` 提供两份公开契约；`examples/default/` 是四载荷全 C 空事实示例；`local/` 与 `history/` 被忽略，只在 init/快照时生成，不作为活动源提交。M4b eval 实际加载 `--pack` 指定活动包，不读取 examples 冒充当前策略。
- `config.example.json`：示例配置，当前只含 `policy_pack`（指向被忽略的本机策略包）；其余 schema 待设计，当前不是 M0 可消费配置。
- `config.json`：当前存在、被忽略的本机配置，保留且不读取内容；不是评测配置。
- `.gitignore`：依赖、缓存、运行产物、个人事实及凭据忽略约定，包含 data/screenshots 指导文档例外。
- `.agents/`：Codex 工作流入口占位，当前只有双指导文档，Skills 待设计。
- `.claude/`：Claude 工作流入口占位，当前只有双指导文档，Skills 待设计。
- `data/`：本机产物目录，含自动生成的 evals 结果/状态子目录、M4b `data/evals/eval-<run-id>/` 报告/凭证与 `holdout-registry.local.json`、`logs/m0-process-<运行 UUID>.jsonl` 每次运行独立过程日志、`runtime/` M1/M2 SQLite 库与 M4c/M5/M6 `runtime/profiles/<profile_id>/runtime.sqlite3` profile 独立库（M6 结构 v3 职位/批次/草稿/决策记忆/调度事件与类型化 apply_job 动作账本；v1/v2 迁移备份同目录）；M3 read 不写业务库，M4a policy 只写被忽略的 `prompts/local/` 与 history，M4b eval 只写被忽略的 `data/evals`，M4c facts、M5 action 与 M6 run 只写 profile 库，采集证据由测试临时目录或被忽略本机路径使用。共享设备门禁在 Windows `LOCALAPPDATA/job-application-openagent/device-guard.sqlite3`，不写仓库。运行产物排除文档覆盖且不提交 Git。过程日志只含固定字段（含 `store/sense/read/policy/facts/profile/device_gate/action/run/eval/decision/adb_hierarchy/adb_screen` 阶段），写入失败不影响 stdout/退出码，但 stderr 固定标记 `m0_process_log_degraded`，不声称完整日志已保存。
- `screenshots/`：本机截图预留位置，当前只含双指导文档，截图不提交 Git。
- `AGENTS.md`：项目事实、完整索引、母本规则原文及命令状态；子目录指南引用本文件。
- `CLAUDE.md`：仅含 `@AGENTS.md`，必须与同目录指南配合使用，不能另存一套规则。
- `.git/`：Git 元数据，排除指导文档覆盖。
- `.venv/`：本机 Python 虚拟环境，被忽略且排除指导文档覆盖。
- `node_modules/`：npm 自动安装的第三方评测依赖，被忽略且排除指导文档覆盖。

`scripts/`、`skills/`、`.agents/skills/`、`.claude/skills/` 当前不存在，按规则不创建；Node 回归测试实际放在 evals，M0–M5 Python 回归测试放在 tests。旧 scripts/main.py 与旧业务 unittest 约定不适用当前仓库。`docs/` 当前已存在，存放公开分析文档、M0–M5 操作手册、M3/M4c/M5 契约与目录指南（见根目录索引），不承载业务代码或测试。`prompts/local/`、`prompts/local/history/` 与 `evals/private/` 被忽略且不预建；`prompts/base/`、`prompts/examples/default/`、`tests/fixtures/policy-preview/` 与 `evals/policy-suite/` 是已交付公开资源，preview/eval 依赖且部署必须携带。`.promptfoo/`、`build/`、`dist/`、`.test-artifacts/`、`test-evidence/` 当前不存在，按规则不创建；未来产生的依赖、缓存和自动生成产物也不补指导文档。

## 三、全局工作规则与编码规范

<!-- RULES:BEGIN -->
### 核心原则：编码前先思考

- 不要假设。不要隐藏困惑。明确暴露权衡。
- 明确说明你的假设。如果不确定，就提问。
- 如果存在多种理解方式，列出来；不要静默选择其中一种。
- 如果有更简单的做法，直接说明。必要时提出反对意见。
- 如果某件事不清楚，就停下来。说清楚哪里令人困惑，然后提问。

### 保持简单

- 用能解决问题的最少代码。不要做推测性扩展。
- 不添加超出请求范围的功能。
- 不为单次使用的代码抽象新层。
- 不添加未被要求的“灵活性”或“可配置性”。
- 不为不可能发生的场景添加错误处理。
- 如果你写了 200 行，而 50 行就能解决，重写并简化。

### 实现前优先级检查

写代码前按顺序检查：

1. 这件事是否真的需要现在做；如果不是当前需求，就不做。
2. 标准库是否已经能解决。
3. 当前平台、框架或命令行工具是否已经提供能力。
4. 仓库已有 helper、模块或已安装依赖是否已经覆盖。
5. 是否可以用一处小改动、一行表达式或配置完成。
6. 以上都不满足时，才写最小可用的自定义实现。

优先删除或复用，而不是新增。简单不是牺牲正确性：如果两个简单方案成本相近，选择边界行为更可靠、数据风险更低的方案。

自问：“资深工程师会不会认为这过度复杂？”如果答案是会，就简化。

### 简单但不偷懒

为了少写代码，不得省略以下内容：

- 信任边界上的输入校验。
- 避免数据丢失、数据污染或错误覆盖的处理。
- 安全相关检查。
- 用户明确要求的行为。
- 非平凡逻辑的可运行验证。

非平凡逻辑优先使用现有测试体系验证；如果现有测试体系过重，可以留下一个最小可运行检查。极小的一行改动不强制新增测试，但必须能说明验证方式。

如果有意采用临时简化方案，必须写明限制、原因和后续改进条件；不要留下看不出边界的隐性技术债。

### 编码规范与目录维护

本仓库只使用这一套编码规范。不要为不同目录、不同语言或不同工具另建一套冲突规则；如果确实需要例外，先在任务说明中写明原因。

- 根目录 `AGENTS.md` 保存全局工作规则。
- 子目录 `AGENTS.md` 不是重复根规则的地方，而是该目录的索引：说明目录用途、列出当前文件/子目录功能，并记录改动后需要同步更新的索引要求。
- 如果新增、删除、改名、移动该目录下的文件或子目录，必须同步更新对应目录的 `AGENTS.md` 索引。
- 每个目录的 `CLAUDE.md` 只包含 `@AGENTS.md`。
- 根目录 `AGENTS.md` 的规则区若有上游母本（例如项目初始化提示词），以母本为唯一来源：修改规则时，必须同时更新母本和所有已生成项目的 `AGENTS.md`，保持逐字一致，不得只改其中一处。
- 不为了满足规范创建空目录。只有当目录承载真实代码、脚本、测试、文档或数据说明时，才创建该目录及对应的 `AGENTS.md` / `CLAUDE.md`。

开发或修改 skills/ 下的 skill 时，必须同步更新 .claude/skills/ 和 .agents/skills/ 的对应运行时入口或镜像，并验证三处指向同一套工作流语义；如果某处使用链接或 wrapper，不要复制第二份正文，但要确保入口仍指向最新源 skill。

上面的 skill 同步规则作为后续相关开发的约束；项目当前没有 skill 时，不为此创建目录。

### 文件注释规则

- 每个源代码文件都必须在文件顶部写明文件用途。
- 文件用途注释要说明“这个文件负责什么”，不要重复文件名。
- Markdown、JSON、YAML 等非代码文件无法自然放置代码注释时，使用标题、description 字段或文件开头说明表达用途，遵守文件格式和已有 schema。

示例：

```python
"""加载并标准化每日 A 股公开数据。"""
```

```typescript
/**
 * 生成每日研究报告的页面状态。
 */
```

### 方法注释规则

- 每个函数、方法或公开脚本入口都必须写注释说明功能。
- 注释重点说明输入、输出和业务含义；不要写“给变量赋值”这类代码本身已经表达清楚的内容。
- Python 使用 docstring；TypeScript/JavaScript 使用 JSDoc；Shell 或 PowerShell 使用紧邻方法/函数上方的行注释。
- 注释必须帮助读者理解用途、边界或业务含义，不写空泛注释，不用注释复述显而易见的语法。
- 新增代码时先满足当前需求；不要因为“以后可能用到”添加抽象、配置或扩展点。

示例：

```python
def normalize_symbol(symbol: str) -> str:
    """把用户输入的股票代码转换为内部统一格式。"""
```

```typescript
/**
 * 将原始行情记录转换为报告生成所需的标准字段。
 */
function normalizeQuote(raw: QuoteRecord): NormalizedQuote {
    // 此处省略实现。
}
```

### 模块文档必须同步维护

模块文档必须同步维护：以后编写或更新任何模块时，必须同时检查并更新该模块所在子目录的 AGENTS.md，使其中的职责、目录结构、入口、接口、约束、测试命令和已知限制与当前代码一致。若新增模块目录且没有 AGENTS.md，必须在同一变更中创建它；若一次变更涉及多个模块，必须同步更新每个受影响模块目录的 AGENTS.md，不能只更新根文档。
<!-- RULES:END -->

## 四、命令与验证

以下均在项目根执行。M0–M6 使用 Python >=3.13 标准库；评测需 Node.js >=22.22.0 与 npm，依赖以 package-lock.json 为准：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m app doctor --fixture tests/fixtures/doctor/ready.json
python -m app store init --db data/runtime/m1-acceptance.sqlite3
python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3
python -m app read --fixture tests/fixtures/read/page.json
python -m app read --fixture tests/fixtures/read/job_list.json
python -m app read --fixture tests/fixtures/read/job_detail.json
python -m app read --fixture tests/fixtures/read/unread.json
python -m app read --fixture tests/fixtures/read/conversation.json
python -m app policy init --dest prompts/local/demo
python -m app policy validate --pack prompts/local/demo
python -m app policy preview --pack prompts/local/demo --case policy-greeting
python -m app policy list
python -m app policy snapshot --pack prompts/local/demo
python -m app policy eval --help
python -m app policy holdout register --batch-id <批次ID> --version <版本> --sample-count <数量> --annotator <标注者> --maintainer <维护者> --candidate-release <候选>
python -m app policy holdout retire --batch-id <批次ID>
python -m app policy acceptance check --pack prompts/local/demo --credential data/evals/eval-<run-id>/acceptance.json
python -m app facts set --profile demo --key work_location --value "<合成值>"
python -m app facts set --profile demo --key work_location --value "<合成值>" --confirm
python -m app facts show --profile demo --pack prompts/local/demo
python -m app facts revoke --profile demo --key work_location --confirm
python -m app facts forget --profile demo --key work_location --confirm
python -m app run check --pack prompts/local/demo --mode read_only --confirm-device
python -m app run prepare --pack prompts/local/demo
python -m app run execute --pack prompts/local/demo --mode read_only --fixture <合成场景> --confirm-device
python -m app device gate inspect
python -m app action card --profile demo
python -m app action eligibility --profile demo --pack prompts/local/demo --mode draft_only --action-kind send_text --target-key <message_key> --conversation-key <conversation_key> --task-key <task_key> --credential data/evals/eval-<run-id>/acceptance.json --provider local-process --model-id <标签> --model-config-hash <摘要> --authorize send_text --confirm-action
Get-ChildItem data/logs/m0-process-*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
git check-ignore data/logs/m0-process.jsonl data/runtime/m1-acceptance.sqlite3 data/runtime/m2-acceptance.sqlite3 data/runtime/profiles/demo/runtime.sqlite3 prompts/local/demo prompts/local/history data/evals/eval-deadbeef/report.json
npm ci
npm test
npm run eval:smoke
npm run eval:model -- "<provider-id>"
```

M4b `policy eval` 的离线正路径需要显式本机桥程序与 `--confirm-external-send`；本 Task 不提供真实 provider 命令，也不实际外发。`policy eval` 失败/零样本/非公开/simulated 不生成验收凭证。M4c `facts`/`policy acceptance check`/`run check` 只使用本机文件、profile 库、临时 OS 根与合成输入；`run check` 不探测设备、不调用模型、不执行动作。

`tests.test_doctor` 覆盖 doctor 正反例、重复 JSON 键、孤立代理字符、畸形 keyguard token、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单 Window 多组件/前后附加内容、提示行清单、设备失败七键映射、stdout 写失败与 `output=failed`、帮助 output/exit、日志白名单/降级故障注入、ADB `started→ok/failed`/中断与 fixture 零子进程断言。ready 夹具命令在日志正常时退出 0、只输出一个 JSON、stderr 为空且不写业务库；每次运行只创建自己的 `data/logs/m0-process-<运行 UUID>.jsonl`，不向共享文件追加。日志写入失败时 stdout/退出码不变，stderr 仅写固定 `m0_process_log_degraded`，表示日志不完整，不能声称完整日志已保存。fixture 模式零设备、零网络、零子进程；真实 `doctor --device --serial <serial> --confirm-device` 只在取得本轮设备确认后手动执行，离线验收不运行。真实模式先取得固定 OS 位置共享门禁，每条 ADB 命令前复核，正常收敛才释放，异常保留并隔离。

`tests.test_process_log_concurrency` 用两个独立 spawn 进程与 Barrier，分别覆盖默认生产目录和显式测试基础路径：两运行必须写不同 UUID 文件，各自按 correlation_id 得到完整、有效、无混行的固定事件序列；显式基础路径不被写入，避免共享追加。`tests.test_store` 覆盖同一候选竞争、不同 owner 全局设备互斥、仅 TTL 过期不释放、priority/created/key 领取顺序、start/renew/finish/recover、blocked 的任务清租约与设备原身份隔离、ReadResult.undetermined/Evidence.unknown 两条路径、旧 nonce/旧截止时间/重复操作、停止未知/异主机/未确认、clock_changed、事件与任务/设备更新未命中/事件写入失败的整体回滚、伪造会话键原子回滚，以及非法类型/枚举、非 UUID owner、跨 app 租约键与 bool 类型；Windows 停止检查器只用 mock ctypes，不读取真实 PID。`tests.test_migrations` 覆盖 v1 STRICT 五表/索引、重复初始化不改业务行、建表中途及版本写入后回滚、初始化写锁 `store_busy` 与释放后幂等、未知版本/非空无版本/schema 篡改拒绝、外键与 check 约束、非 SQLite 文件，以及 `store init` 固定 JSON、默认路径、路径越界、损坏/篡改库和失败日志。`python -m app store init --db data/runtime/m1-acceptance.sqlite3` 连续两次退出 0、只输出固定成功 JSON，第二次只核验且不改业务行；数据库写在被忽略的 `data/runtime/`。

`tests.test_sensor` 覆盖 M2 合成 profile/manifest、正例独立键算法、区域角标边界与行字段排除、区域巨大整数、坏数字节点几何优先、超大行坐标不逃异常、四源正/零/未知冲突、全局标签语义、预览基线、身份缺失/重复、分页最长整行重叠、成功页 null snapshot、完整遍历/全局零覆盖、过期/未来快照、未标定版本、非法 XML/DTD/ENTITY/Unicode/嵌套 hierarchy、manifest/profile 重复 JSON 键、路径越界/链接、非法基线摘要与有序 stop_reason。`tests.test_sensor_store` 覆盖 `sense` 正例两次执行、发现总数 1 且重放不追加事件、唯一 unread 候选、终态同指纹不重开、终态旧键与不同 queued 新键、四类停止原因、未标定/坏输入/非法基线/巨大区域/非法 Unicode 不创建业务库、parse 三态日志、聚合/停因成功与内部失败轨迹、日志降级、超大坐标 CLI 固定 JSON、仓储错误三键安全出口、严格参数、同一日志实例/correlation 与零设备/网络/子进程/任务租约写入口断言。`python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3` 连续两次退出 0，输出 ok/present、唯一 unread、stop_reason=null，重放发现仍 1 且无额外事件；数据库写在被忽略的 `data/runtime/`。

`tests.test_read` 覆盖 M3 五类 `read` CLI fixture、页面分类/unknown 门禁、职位卡规范化/重复身份/详情截断、未读行身份与像素后备、阈值/分辨率/损坏/未知格式、字段阶梯前级优先/冲突/failed/视觉默认关闭/人工、会话身份/方向/稳定消息键/跨屏重叠/新增消息/过期、ReadResult 值类型分派、M1 仓储拒绝非 UnreadSnapshot、定向核对与 undetermined 门禁；`tests.test_screen` 只 mock `subprocess.run`，覆盖 dump/cat/screencap 参数、`shell=False`、5 秒超时、不重试、证据保存/失败、共享门禁先于采集。`python -m app read --fixture tests/fixtures/read/conversation.json` 在当前合成输入下退出 0、`actionable=true`、`inbound_state=known`；页面/职位/未读 fixture 同样接生产解析与阶梯逻辑，不写业务库。

`tests.test_policy_contract` 覆盖 Envelope/Decision 键/类型/词表/回显/证据/nonce 租约参数/2048 字节边界、嵌套重复 JSON 键、H 无规范化与四类摘要无自引用、64 KiB 渲染上限、职位五个 intent、围栏拒绝、披露追加后的码点/控制字符/联系方式/禁谈/事实复验；`tests.test_policy_pack` 覆盖 TOML+三 md 完整 schema、45 组合唯一覆盖、同档重复/跨档冲突/漏项/A 越权、硬下限、有效限制、联系方式、system/developer/user 投影与 preview 案例 schema；`tests.test_policy_store` 覆盖路径/普通文件/链接/BOM/NUL/大小/CRLF、`policy init/validate/preview/list/snapshot`、编辑语气/分档/披露、清单与复制摘要、restore 三值/base/清单/快照/目标篡改、坏 profile 库 unknown 拒绝、合成 stopped 正路径、失败清理、四键外层、参数错误区分、日志 stage/operation/结果与零设备/网络/子进程。`policy` 只用临时项目根与固定合成 base/包/preview，不写仓库 `prompts/local`；生产 restore 已接真实活动检查，测试以坏库和合成占用覆盖 active/unknown 拒绝。

`tests.test_profile_facts` 覆盖事实提案不落库、确认后重读再显示、程序来源 ID、有效期、同键冲突/显式 replace、撤销排除、forget 无值审计、联系方式/控制字符拒绝、值不进日志、包白名单待办、pack/profile 身份一致、不同 profile 不共享库、旧合成库不导入与元数据绑定；`tests.test_device_gate` 覆盖固定 OS 路径、不同 profile/不同逻辑项目根互斥、完整 owner/profile 篡改、未知版本/额外对象不改写、跨进程竞争、崩溃/PID/TTL 不释放、异常隔离、nonce 篡改、库不可读、非 Windows 拒绝、profile 库 leased/running/blocked 活动检查；`tests.test_run_guard` 覆盖 read_only 本轮确认、draft 当前验收、A 档固定拒绝、预检零子进程、跨库故障隔离与门禁/profile 日志配对；`tests.test_profile_session_lease` 覆盖合法 profile 库接入唯一 SqliteStore、领取/续租/失效与 decide 桥前后拒绝；`tests.test_m4c_closed_loop` 覆盖真实活动 restore、目录路径不发布、三值一致、同 profile 事实/任务审计不重置、旧凭证摘要变化拒绝、唯一仓储快照桥前后重核与实际 CLI 合成桥 eval→snapshot→restore→validate/preview→eval 串行闭环。

`tests.test_bridge` 覆盖 A/B 真实参数数组子进程成功、固定 system/developer/user 三消息、无工具无历史、伪造 role 保持 user 数据、真实启动失败一次重试后固定失败、提交后 20 秒超时不重试、认证/额度/断连/坏 JSON/重复键/角色不符/坏回显固定诊断、未确认事实回复拒绝、包变动与桥前后租约过期、RenderedRequest 篡改先拒绝。`tests.test_policy_eval` 覆盖实际加载当前包、公开套件模拟与真实适配器路径报告、base/pack/profile/provider/model/model_config_hash/suite_hash 绑定、模拟与失败无凭证、成功凭证、授权拒绝前零桥调用、编辑 pack 改变 developer/prompt_hash/pack_hash、24 行映射与旧 24 文件不变、公开/回归/留出分桶、留出冻结/访问/同摘要重跑非新证据/摘要变化拒绝/退役/新批次/外部 provider 拒绝/零样本未测、CLI eval help/echo/未确认与 holdout register/retire。M5 `tests.test_action_domain` 覆盖动作键/观察→三态/授权/人工卡；`tests.test_action_store` 覆盖 pending 先落库、leased 未 start 零准入、三态与设备处置、事件/任务/设备故障回滚、过期租约、崩溃 pending、UTC 跨日/额度/冷却/调低/重启、confirm/abandon 与 v1→v2 备份迁移；`tests.test_action_usecase` 覆盖授权精确、无账本（含 IME 预读）零子进程、真实合成子进程+真实账本 performed/done、证据索引 before/readback/after 失败三态与任务/设备处置、unknown 单次调用、零重放与当前证据；`tests.test_action_executor` 覆盖 ADB 参数数组/5 秒/不重试/每命令门禁与合成执行器；`tests.test_action_cli` 覆盖五动作正反例、资格正例/冷却负例、人工卡/对账/abandon 隔离与 restore unknown。所有 M5 决策/回复均为合成观察与 mock；M4c 真实租约只接合成 profile 库，真实 provider、私有留出、真机动作与生产租约未运行。

npm ci 安装锁定依赖；npm test 运行 Node 内置正反例检查，无需安装 promptfoo；smoke 需先安装依赖，仅回放合成参考答案，不请求模型。model 必须指定真实 provider 并按其类型配置认证，可能产生调用费用；本次未选择或调用实际模型。M4b `policy eval` 是独立 Python 入口，只接收显式 A/B 本机桥程序；离线测试注入模拟 transport 仍走同一授权回调，simulated 不生成凭证。M4c `run check` 是预检；M5 `action` 只在合成执行器/临时 profile 库上验证，不执行真实 ADB、设备动作或真实发送。M6 调度与完整 run 测试/独立构建/lint 命令尚未实现，不执行旧 scripts/main.py/unittest 命令。

本次已执行 `python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen`（227 个测试，含子测试断言；退出 0）、`python -m app doctor --fixture tests/fixtures/doctor/ready.json`（退出 0）、`python -m app store init --db data/runtime/m3-regression.sqlite3`（退出 0、固定 JSON）、`python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m3-regression.sqlite3`（退出 0）、`python -m app read --fixture tests/fixtures/read/{page,job_list,job_detail,unread,conversation}.json`（均退出 0、接生产解析/阶梯、零设备/库/子进程）、`npm test`（52 项通过）；主端独立脚本串行执行为阶梯 11/11、身份 15/15、CLI/像素 27/27、CLI 负例 5/5（并行运行会因共享默认日志目录产生额外 created 文件，不作为产品缺陷）。另完成工作树 `git diff --check`、基线 `git diff --check cc103f8bd479ccecead33af71ffd1a429e1cbd4a <新SHA>`、`compileall` 与敏感路径静态检查。M3 测试覆盖页面/职位/未读/会话、字段阶梯来源槽位/前级优先/冲突/failed/视觉默认关闭/人工/跨上下文自校验/派生结果篡改/保留冲突或失败证据伪造拒绝、会话身份与方向/重复正文与方向控件/稳定消息键/重复 ID 会话未知/后页重复跨页传播/跨屏重叠/新增消息同会话核对/过期、公开解析入口标定/context/合并值类型/profile 校验、不支持标定边界零 pixel 事件/实际像素 started→failed、错页零 pixel 日志、像素阈值/分辨率/损坏/未知格式/显式零/像素 count/日期/来源/colorspace、undetermined/identity/gate/证据保存失败、定向核对重放/陈旧/错 profile、序列化唯一归属、ReadResult 值类型分派、M1 仓储拒绝非 UnreadSnapshot、共享门禁先于采集与 mock ADB 参数/超时/不重试。它们验证 M0 判据、M1 合成库/租约/回滚、M2 合成解析/落库/停因、M3 离线读取与评测链路，不代表真实模型、真实设备、运行时调度、真机像素/稳定标识标定或真机只读一轮。新增修改后须运行受影响检查，不能沿用该记录当作新版本验证。

M4a 返修后已执行 `python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen tests.test_policy_contract tests.test_policy_pack tests.test_policy_store`（343 个测试，125.717s，退出 0；M4a 116 项）、`npm test`（52 项通过）、`python -m compileall -q app tests`、`git diff --check` 与 `git check-ignore`；另在合法 CRLF 的独立副本运行同一 343 项（123.672s）全部通过；以基线 `584b81495c8cb988a8722b082cca9da0181f3c7a` 的 `HEAD:AGENTS.md` 提取 `RULES:BEGIN/END` 区与工作树逐字比较，均为 2406 字符且一致，未改全局规则。另在临时项目根实际执行 `policy init` → 编辑 tone/披露 → `validate` → `preview` → `list` → `snapshot` → 同一生产 `restore_pack` 注入明确合成 `stopped` → `validate` → `preview`，均 ok 且恢复 pack_hash 与清单一致；生产 CLI `restore` 无可信停止证据返回 `policy_changed` 并记 `undetermined`。M4a 测试覆盖 Envelope/Decision 严格键/类型/词表/回显/证据/租约参数/2048 字节、嵌套重复 JSON 键、四类摘要无自引用与 H 无规范化、唯一 {base,pack,data} 内容对象 64 KiB 边界、TOML+三 md 完整 schema、45 组合覆盖/重叠/漏项、2 A/4 B/39 C 统一硬下限、路径/链接/BOM/NUL/大小/控制字符/联系方式（含可选双指南）、包内事实模式与回复事实值严格匹配（保留小数/范围/单位/地点全文，经验须明确单位、普通数量问句不误判、未知数量级受控 conflict）、subject 次要空字符串、披露追加与禁谈复验、system/developer/user 投影、事实交集与重复/冲突、init/validate/preview/list/snapshot/restore 三值、复制前后源重读、清单/快照/目标篡改、失败清理与不覆盖、生产受控拒绝、逻辑停止正路径、四键/配置错误外层、链接候选 list、load/validate/render/publish 阶段日志与零设备/网络/子进程。该记录不代表 M4b/c 真实桥/评测/运行绑定、真实模型、真实设备、事实确认入口、共享门禁或发送授权已验收。

M4b 返修后已执行 `python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen tests.test_policy_contract tests.test_policy_pack tests.test_policy_store tests.test_bridge tests.test_policy_eval`（403 个测试，58.794s，退出 0；新增/扩展 M4b 60 项）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore` 与敏感路径静态检查；RULES 区与固定基线逐字一致，旧 24 案例/参考回放未改。M4b 测试覆盖可信时钟桥前/重试前/桥后流逝、启动前到期零调用、桥期间跨截止 1/1、重试前跨截止/包变化/事实到期 1/0、临时创建/输入写超预算零 Popen、2MiB stdout/慢进程/不读 stdin/大 stderr/短命继承 stdout 回收、输出监测/wait OSError；A/B 角色分区与固定诊断；实包 eval 报告/correlation/分桶、编辑包改变请求摘要、模拟/失败/零样本/缺套件无凭证、失败原子清理与 junction 拒绝、缺 public/authorize 日志唯一失败终态、public/holdout 不受无关坏 regression 准入影响；留出 intent 先持久化/并发/中断/收尾写失败/样本身份换名与子集拒绝/退役实际 regression 准入/登记失败保留未确认准入但过滤不可运行/无上传合同绑定/零样本未测；job_card 正控制与反例。该记录不代表真实 provider、私有留出、M4c 真实活动/事实确认、M5 授权/动作或真机已验收；未实际模型外发。新增修改后须运行受影响检查，不能沿用该记录当作新版本验证。

M4c F01–F03 集中返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（463 个测试，121.150s，退出 0；含 profile 事实/共享门禁/恢复/预检/真实会话租约/CLI 闭环模块）、受影响 8 模块（`tests.test_profile_facts tests.test_device_gate tests.test_run_guard tests.test_m4c_closed_loop tests.test_profile_session_lease tests.test_doctor tests.test_screen tests.test_policy_store`）149 项通过、`npm ci`、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore` 与敏感路径静态检查；RULES 区未改，旧 24 案例/参考回放未改。M4c 测试覆盖事实提案/确认/再显示/冲突/撤销/遗忘/无值审计/值不进日志/旧库不导入、固定 OS 门禁跨进程与跨逻辑项目根互斥、崩溃/PID/TTL/nonce 篡改/库不可读/异常隔离、restore 的 active/unknown/stopped、read_only 本轮确认、draft 当前验收、A 档固定拒绝、跨库故障隔离、三值一致闭环、同 profile 事实/任务审计不重置、旧凭证摘要失效、唯一仓储快照桥前后重核与真实 profile 租约领取/续租/失效。该记录不代表真实 provider、私有留出、真机、真实 OS 门禁目录、M5 动作/调度或完整 MVP 已验收；未实际模型外发或连接设备。

多 agent 共用设备加固后已执行 `python -m unittest discover -s tests -p "test_*.py"`（473 个测试，108.199s，退出 0；在排除 `data/` 的工作树干净副本运行，因原工作树 `data/` 已被并行编排写到约 163 万文件，`tests.test_doctor._sqlite_files()` 的全仓 rglob 在原工作树不可用，属本机数据目录问题）、`tests.test_device_gate tests.test_profile_facts`（41 项）、`tests.test_policy_store tests.test_run_guard tests.test_profile_session_lease`（65 项）、`python -m compileall -q app tests`、`npm test`（52 项通过）、`git diff --check` 与本机 `python -m app device gate inspect` 只读调用（退出 0、报告无占用）；RULES 区未改。新增测试覆盖只读 `device gate inspect` 的 free/held/quarantined、门禁路径为目录、不可识别状态与非 Windows 不可用且缺失时不建库，以及同 profile held/quarantined 拒写、提案/`show`/其他 profile 放行与拒写日志 `gate` 类别不含事实值。该记录不代表真机、真实 `LOCALAPPDATA` 崩溃恢复、多机并行或 M5 动作对账已验收。

M5 第2轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（560 个测试，113.249s，退出 0；含 5 个 M5 模块与全部既有模块）、`npm ci`、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。H01–H07/N01 覆盖：默认动态时钟与验收四摘要、执行后失效冻结 unknown/blocked、当前租约唯一核验/合法真实续租、唯一姓名+职位/editor/非滚动列表/可点槽位/目标子树差分、持久 IME 绑定与早退恢复、同 host 恢复/父子命令检查器、日志白名单/提交配对/XML 证据索引、重复 Evidence/租约/pause 收口。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第4轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（577 个测试，133.116s，退出 0；含全部既有模块）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。J01–J04 覆盖：公共缺身份零子进程、公共+真实 ADB 适配器错职位不可逆 tap0、联系方式类型专属效果、dialog 槽位 disabled 拒绝、删除 before/dialog/after 独立覆盖与完整列表 removed 正例、任意节点 false 与不完整视口拒绝；`run_adb` 实际回调记录命令族后代并通过末端存活拒绝、缺失/坏行/枚举失败 unknown、身份写失败有界 kill/wait、超时 kill 与回收预算；设备对账坏 XML 拒绝后同 host 正例成功；采集保存与 guard 拒绝 started/单一终态。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第5轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（586 个测试，154.639s，退出 0；含全部既有模块）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。J01/J02/J04 覆盖：结构化 `ActionIdentity` 缺失/冲突在 pending 前拒绝、公共+真实适配器目标不符 tap0、嵌套槽位单一定位与另一类型已处理不污染、真正列表容器覆盖与行/锚点 false 拒绝、完整列表 removed 正例；公开入口首次 IME 前绑定并记录命令族、晚生后代结束扫描记录并拒绝存活、部分写/枚举失败/未闭合 unknown、身份写失败与超时有界回收、全停仍可 stopped 正例。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第6轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（593 个测试，179.893s，退出 0；含全部既有模块）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。J01/J02/J03/J04 覆盖：列表容器内其它行裁剪、外层可见区域裁剪、缺失/坏/零面积/自相矛盾 bounds 拒绝与完整范围正例；同分组两个同类型槽位歧义零点击、动作后按钮 disabled 仍证明本类型新状态、另一类型已处理不污染；Windows Job Object 平台有界未证明/空族/不可用三态与真实账本闭合；任务身份 SELECT 读故障 started→单一失败终态、零设备/零 pending、日志不降级。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第7轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（597 个测试，191.669s，退出 0；含全部既有模块）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。J01/J02/J04 覆盖：容器内有可见意义后代缺失/坏/零面积 bounds 与更外层裁剪/滚动三阶段拒绝、完整正例保持；命令族 Popen 前建 Job、CREATE_SUSPENDED 启动后加入再恢复，权限 SET_QUOTA|TERMINATE，mock 不触真实 Job；本机自有合成 Python 子孙真实 Job 正常停止、平台有界/未证明/不可用三态；命令族未证明全停时公开观察保留已证效果但 unknown、gate_converged=false、人工卡非空。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第8轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（605 个测试，155.346s，退出 0；含全部既有模块）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。J01/J02/J04 覆盖：列表内中间与更外层裁剪/滚动、content-desc 可见叶节点缺失/坏/零面积 bounds 三阶段拒绝与完整正例；执行起点前未闭合事实、assign/resume 失败不执行、二次超时与 OSError 有界 finally、完成写失败与空 IME 早退保持 command_quiet=false、动作 unknown/任务暂停/gate_converged=false/人工卡；真实自有合成 Python 子孙 Job 正常停止保持。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第9轮返修后已执行 `python -m unittest discover -s tests -p "test_*.py"`（606 个测试，120.029s，退出 0；含全部既有模块）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore`、敏感路径静态检查与工作树 `RULES` 区与 HEAD 逐字比较（未改规则）；旧 24 案例/参考回放未改。J02 日志出口覆盖：一次真实 complete 追加 OSError 准确 hit1，`action_finish_command` started→failed 固定安全终态（store_io_error/store）、同一 ProcessLog 可关联、未降级且不含异常原文，公开仍 unknown/gate_converged=false/人工卡/智能隔离，已证 performed/sent 效果保留；正常与可靠全停路径 finish 为 started→ok，无虚假 failed；空 IME/无平台 unknown 对照保持。该记录不代表真实手机、ADB、输入法/坐标/附件名/联系方式/删除保护/恢复时序、真实 provider、私有留出、M6–M8 或完整 MVP 已验收；合成授权与资格为真不构成真实发送或 A 档授权。

M5 第10轮集中返修后已执行 `python -m unittest tests.test_action_domain tests.test_action_store tests.test_action_usecase tests.test_action_executor tests.test_action_cli tests.test_device_gate tests.test_screen tests.test_run_guard tests.test_profile_facts tests.test_m4c_closed_loop`（213 个测试，34.304s，退出 0）、`python -m unittest discover -s tests -p "test_*.py"`（622 个测试，107.493s，退出 0）、`npm test`（52 项通过）、`python -m compileall -q app tests` 与 `git diff --check 08aad36aa987220e959fb9d38718071c933a1729`（退出 0）；`npm run eval:smoke` 未运行（本工作树未安装 promptfoo，避免未确认的网络安装）。新增独立判据覆盖：无账本/未绑定/错绑定与绑定后 IME 预读零子进程；真实临时 profile + 生产 `perform_action` 无账本 pending/blocked/隔离；真实合成子进程 + 真实 Job 账本 performed/done；证据索引 before/readback/after 真实目录冲突下的 tap 数、三态、命令/现场收敛与任务/设备处置；错 profile/坏库迁移前拒绝、既有备份保留与合法事实保留；read_only reconcile 零 profile/门禁/设备写入。该记录不代表真实手机、ADB、输入法/坐标、真实 provider、M6–M8 或完整 MVP 已验收；合成执行器与 mock 不能冒充真实设备动作或命令族停止证明。

M5 固定候选串行集成与事实写竞态修复后已执行：从干净 `9628690` 正常 merge `b2ea93f`，解决 10 个文本冲突并修复无文本冲突但已断开的 `device_gate.recover`（`self._read_row` → 模块级 `_read_gate_row`）；确认事实写改为固定门禁库 `facts_guard` 写事务覆盖最终状态校验、profile 打开/迁移与 set/revoke/forget 提交，状态先于 profile 比较验证，acquire 零等待并把 `SQLITE_BUSY` 立即映射 `device_busy`；`ExitStack` 保证 profile close 失败也先释放守卫事务并传递真实异常，body 失败不记成完整成功。`python -m unittest discover -s tests -p "test_*.py"`（647 个测试，123.532s，退出 0）、受影响 12 模块（297 个测试，51.182s，退出 0）、`python -m unittest tests.test_device_gate tests.test_profile_facts`（57 个测试，8.106s，退出 0）、`npm test`（52 项通过）、`python -m compileall -q app tests`、`git diff --check` 与 `RULES` 区对基线 `9628690` 逐字比较（2513 字符一致，未改规则）。新增独立判据覆盖 set/revoke/forget 同 profile 拒写零消费、v1 迁移与首次 profile 创建零写入、首确认初始化严格空门禁库、跨 profile 不可识别状态失败关闭、异常/进程死亡与 profile close 失败均释放 SQLite 锁、不同 profile 短暂串行化与 release→acquire 切换、acquire 零等待立即 `device_busy`、坏 schema 不改写与真实 `recover` 正反例。`npm run eval:smoke` 未运行（本工作树未安装 promptfoo，未安装或调用真实 provider）。该记录不代表真实 `LOCALAPPDATA` 并发/崩溃、真机、真实 provider、M6–M8 或完整 MVP 已验收；也不声明事实写具有跨进程线性化以外的审计保证。

M5 集成 busy 异常收敛返修后已执行：真实 SQLite `BEGIN EXCLUSIVE` 竞争下，公共端口不再泄漏内部 `_GateBusy`——acquire 零等待 `device_busy`，verify/release/recover/session_identity `device_busy`，inspect/inspect_gate_occupancy 保守 `device_gate_io_error`，activity 收敛 unknown；busy 不释放占用、不改 owner/nonce，日志 started 对应 failed，锁解除后同一持有者正常复核并明确 release。`python -m unittest tests.test_device_gate.GateReadOnlyTests.test_exclusive_sqlite_busy_is_mapped_at_public_ports`（1 个测试，4.156s，退出 0）、`python -m unittest discover -s tests -p "test_*.py"`（648 个测试，108.421s，退出 0）、受影响 12 模块（298 个测试，55.269s，退出 0）、`npm test`（52 项通过）、`python -m compileall -q app tests`、`git diff --check` 与 `RULES` 区对基线 `9628690` 逐字比较（2513 字符一致，未改规则）；协调端独立脚本 `independent_gate_busy.py` 复跑输出 inspect CLI `device_gate_io_error`、verify/release `device_busy`、`adb_calls=0`。`npm run eval:smoke` 仍未运行（未安装 promptfoo，未安装或调用真实 provider）。该记录不代表真实 OS 门禁并发/崩溃、真机、真实 provider 或完整 MVP 已验收。

M6 调度、安全点、批次预算、身份游标、实际 `run prepare|execute`、profile v3/草稿/决策记忆与 `apply_job` 边界实现后已执行：`python -m unittest discover -s tests -p "test_*.py"`（706 个测试，164.523s，退出 0）、`python -m unittest tests.test_runtime_domain tests.test_runtime_store tests.test_runtime_execute tests.test_runtime_apply`（51 个测试，20.249s；另有 apply/policy/module 证据测试）、`python -m compileall -q app tests` 与 `git diff --check`；`npm test`/`npm run eval:smoke` 按本轮剩余验证执行记录（见交付报告）。新增独立判据覆盖：七个安全点条件逐个为假；未知未读一次定向核对后暂停；职位→未读实际切换才记录 `stage_preempted`，首次观察保留、`waited_ms` 由单调时钟计算；900 秒/200 调用/20 候选等号停止；第 6 次抢占暂停；重启/新进程不回补预算、不重复已访问职位；游标重排/插入/消失；检查点事件故障回滚；v1/v2→v3 备份与身份/DDL 校验、旧动作行保留；typed action 任务引用数据库约束；`apply_job` performed/applied、unknown 隔离不重放、CLI 配额合同缺失零写入；read_only 零业务写/导航/模型；显式 prepare 在共享门禁占用时 `device_busy` 且不改 profile；坏场景缺键/额外键/答案键失败关闭。该记录不代表真实手机/ADB/输入法/坐标、真实 M3 标定与只读一轮、真实 provider/私有留出、真实 OS 门禁并发/崩溃、`apply_job` 生产配额合同、M7 清理、M8 灰度或完整 MVP 已验收。

文档维护的一次性校验仍按初始化提示词：比较母本规则区两个标记与全文、8 小节顺序、Python/TypeScript 各 2 个示例，并检查自有目录的双指南、精确大小写、引用与直接子项索引；排除 Git、虚拟环境、node_modules 和 data/evals 等自动生成产物。此类校验不是业务测试。

M6 B1–B8 集中返修后已执行（详见交付报告）：生产 `run execute` 改为 M3 `load_and_read_fixture`/`is_actionable` → `RepositorySnapshot`/`ProfileSessionLease` → 唯一 `decide`；缓存必须命中摘要+草稿并重核 facts/policy/lease；needs_human/invalid 不标 done；resume 原子恢复；七安全点由实际读取/pending/checkpoint/现场派生；预算负超支真实记录；apply_job 经显式独立配额合同、perform_action、CommandIdentityStore/执行器 E2E；策略 `promote` 增加 Windows winerror 5/32 的 3 次/50+100ms 有界 rename 重试。原35完整流程责任与五项 MODULE_EVIDENCE 已在同一作者范围内核对：新增模块证据覆盖 invalid lease CHECK、FK/事务回滚/RESTRICT、active 唯一冲突、缺本轮确认零外部工作、操作手册文档证据；未把模块证据计为场景 PASS。真实设备/provider/private/MVP 仍未测。

M6 b4 固定返修（原树 `bin448482/boss-m6-runtime-1125`）最终候选已执行：`python -m unittest discover -s tests -p "test_*.py"`（724 个测试，171.325s，退出 0）、`tests.test_runtime_execute tests.test_runtime_apply tests.test_runtime_store tests.test_runtime_domain`（62 项，退出 0）、`tests.test_policy_store tests.test_migrations tests.test_operator_docs`（75 项，退出 0）、`tests.test_action_executor tests.test_action_usecase`（95 项，退出 0）、`npm test`（52/52）、`python -m compileall -q app tests` 与 `git diff --check`（无空白错误）；RULES 区 LF 归一后仍为 5622 bytes / SHA256 `4f3b25d7a46f59e6b26d16216ebcb19e847b22e1baa976d514a6a6622f0448c3`。独立合成探针复核：fake job/cleanup/read_only hint 均不产生候选、claim、计数或隔离；缓存证据身份变化 `reused=0` 且重新生成草稿，一致损坏为 `semantic_block`，命中 `decide=0/bridge=0`、`snapshot=1/lease=1`，未命中 `1/1/2/2`；七安全点位置 4/5 不再恒等；unknown 未读暂停 `unread_unverified` 后恢复处理实际职位且无 pause；20 秒路径 `renew=1`、逐步骤 `gate.verify=3`；publish 跨实例/替换/嵌套链接均 `invalid_input` 且目标未发布，持续句柄耗尽后暂存保留；普通 M1 job-key `invalid_input`、同事务 rollback/reopen 为 0；`operator_docs_report` 缺失/不完整控制返回 BLOCKED；正常 run 后批次 completed、restore stopped，paused/running/queued 仍 unknown/active。旧 B7 探针的 fixture `device_calls=250` 现按新要求不再作为预算来源，实际读取计数与负超支由具名 `test_negative_overrun_uses_actual_reads` 覆盖。该记录不代表真实手机/ADB/输入法/坐标、真实 M3 标定与只读一轮、真实 provider/私有留出、真实 OS 门禁并发/崩溃、`apply_job` 生产配额合同、M7 清理、M8 灰度或完整 MVP 已验收。

M6 ffc 最终独审 changes_requested 后，原树继续完成 B8/B1 余留修复：`ActionObservation` 增加 `apply_completed_before`，真实 `AdbActionExecutor` 在 before 已明确“已申请/继续沟通”时于不可逆 tap 前 0 tap 拒绝，`_precondition_ok`/`_performed_effect` 要求 before 未完成→after 明确完成；全局 snapshot 差异、既有 done 或目标外 sibling 变化不再证明本次 applied。`new_message` 第二级改为读取 M2 `sense` 已持久化的 queued `hr_discoveries` 并经同键实际 M3 `Conversation` 复核，同会话未读优先/旧 queued 不重复；兼容 `new_messages`/`unread_unknown` hint 不再决定任务、计数或暂停。最终候选全量 Python 为 734 tests / 385.268s / exit 0，新增 apply before-done/已点击无新鲜完成、实际 M2 解析+upsert new_message、hint-only、同会话未读优先与 queued 无会话失败关闭等具名证据；LF 归档复跑、npm、compileall、diff-check、RULES 与精确归档 manifest 见交付报告。真实设备/provider/private/MVP 仍未测，不构成独立复核或场景 PASS。

M0–M6 已完成离线实现；M3 真机标定与只读一轮、M4c 真实 OS 门禁并发/崩溃/真机、真实 provider/私有留出、M5 真机动作/真实恢复时序、M6 真实调度等待/真机导航/真实模型、`apply_job` 生产配额合同、M7 清理、M8 灰度与完整 MVP 均未测。后续仍需按里程碑补充真实运行绑定、业务范围与授权模型、运行时决策记忆、事实对账开锁、业务配置 schema、设备适配及真实业务验证。确定后同步更新各受影响指南。

M6 G1/G2/G3 生产边界补齐（活动分支，基线 c4）已执行：`python -m unittest discover -s tests -p "test_*.py"` 为 923 tests / 387.666s / exit 0（基线 879 + 新增/扩展 44）；`python -m unittest tests.test_runtime_domain tests.test_runtime_execute tests.test_runtime_store tests.test_runtime_apply tests.test_runtime_g123 tests.test_read` 与 `tests.test_offline_scenarios`（145 tests）在活动工作树通过。新增具名证据覆盖：唯一 `derive_safe_point` 空 checkpoint 与真实提交、检查点事件故障回滚、A/B 最近动作不互相洗白/阻断、空/非法身份与尚未 start 的区分、真实 pending 与已提交分离、已收敛历史不阻断新轮；`verify_unread` 普通读取先按可信单调钟结算后复核预算/双钟/门禁，再实际一次 M3 定向采集；真实设备调用/耗时/亚秒余量、旧 hint 反控、全覆盖 absent 与 viewport/count-only/过期/未来/类型不符；首次观察取真实采集完成时刻（含定向首次带来 present 未读）；S01 安全点 20020→真实开始 20200/wait 10200 与安全点前零切换、S08 三窗口（含原始可见性切换）arrival/observation/start 分离、未 start 零 `stage_preempted`/零抢占、安全点后漂移/预算耗尽阻断 claim；S05 两卡游标连续刷新/重排不重复；S06 已交付唯一 AdbRuntimeRead 的 run_adb.on_spawn 实际创建计数与真实 execute_run 199/200/201/250 矩阵；长 read 逐命令续租仍待补；S07 1001ms 详情读取正常 finish、20/21 秒最新 Lease renew+finish、25 秒桥后 discovery done、start 后 nonce 变化零详情读取、终态后零续租。无数据库 schema 变更、无新增 CLI 公共字段；场景侧 `tests/scenarios/**`、`tests/test_offline_scenarios.py` 与私有回归未改。S06 适配器与真实 199/200/201/250 矩阵已交付，长 read 逐命令续租仍待补；双钟安全停止在漂移前先结算可信单调耗时，保存最后可靠游标/四维预算，错误 batch/cursor/原因及事件故障回滚负控通过。该记录不代表真机、真实 provider、真实 OS 门禁并发/崩溃、`apply_job` 生产配额合同、M7/M8 或完整 MVP 已验收；最终固定 raw Git 全量回归、npm、compileall、diff-check、RULES 与指定独审/合并后复验见交付报告。
