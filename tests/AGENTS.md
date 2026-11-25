# tests 目录指南

## 一、目录用途

存放 Python 标准库 `unittest` 测试与纯合成夹具。当前覆盖 M0 `doctor`、M1 状态库/租约、M2 合成传感器/落库、M3 离线读取/像素/跨屏身份/ADB 采集模拟、M4a 决策契约/策略包/文件事务、M4b A/B 桥/评测报告/留出生命周期、M4c profile 事实/共享门禁/验收复核/运行预检/闭环，以及 M5 五动作判据/动作账本/额度/合成执行器/人工对账/CLI 闭环；不连接真实设备、不调用真实模型或网络、不读取根 `config.json`。M1/M2 测试只使用临时合成 SQLite 库与合成 XML，M3 还使用合成 RGBA 与 mock subprocess，M4a 使用临时项目根与固定合成 preview 案例，M4b 使用合成本机桥桩与模拟 transport，M5 使用临时 profile 库、合成动作脚本与 mock ADB。

## 二、当前文件与子目录

- `__init__.py`：声明测试包，支持按模块运行。
- `test_doctor.py`：M0 CLI 正反例、配置/夹具校验、重复 JSON 键与孤立代理字符、业务状态映射、keyguard token、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单 Window 多组件/前后附加内容解析、设备失败七键映射、stdout 写失败与 `output=failed`、正常帮助 output/exit、过程日志白名单/降级故障注入、CLI 运行窗口内新建的同期日志哨兵不被清理、ADB `started→ok/failed` 与中断仅留 started、调用协议与 fixture 零子进程断言。
- `test_store.py`：M1 合成 ReadResult/Evidence 入库、未知行跳过、同指纹重放静默、queued 取消/延迟、kind 升级、优先级与 kind 过滤、竞争与 TTL、start/renew/finish/recover、blocked 设备原身份隔离、旧 nonce/截止时间、clock_changed、三类事务故障注入、伪造会话键原子回滚、只读查询写锁 `store_busy`、非法 host 的 open_store 失败日志且不建库、仓储打开/操作日志 started/ok/failed 与不回显非白名单元数据、默认不注入 open 成功/失败文件隔离、默认构造无实例降级告警、默认/注入成功操作降级的一次告警与业务隔离、严格输入与 Windows ctypes 停止判定 mock。
- `test_migrations.py`：v1 STRICT 五表/索引、幂等不改行、建表中途与版本写入后回滚、未知版本/非空无版本/结构篡改拒绝、损坏文件、schema 约束、同事务先成功写父行再 FK 失败回滚与 close/reopen 仍为空、初始化/打开写锁 `store_busy` 与释放后幂等、`store init` 固定 JSON/默认路径/路径越界/损坏与篡改库/失败日志。
- `test_process_log_concurrency.py`：两个独立 spawn 进程与 Barrier 同时写默认生产目录与显式基础路径，断言各自独立 UUID 文件、按 correlation_id 的完整事件序列、JSON 有效、互不覆盖；清理只删子进程明确返回的路径，并以合成哨兵断言不误删同期文件。
- `test_sensor.py`：M2 单页解析、区域角标/TextView/ASCII 整数与巨大整数边界、坏数字节点几何优先、超大行坐标不逃异常、四源正/零/未知冲突、全局标签语义、预览基线、身份缺失/重复、分页最长重叠、成功页 null snapshot、覆盖率、过期/未来快照、未标定 profile、非法 XML/DTD/ENTITY/Unicode/嵌套 hierarchy、manifest/profile 重复 JSON 键、路径越界/链接、独立键算法、领域聚合与有序 stop_reason。
- `test_sensor_store.py`：`sense` 正例两次重放、发现数 1 且不追加事件、终态同指纹不重开、终态旧键与不同 queued 新键、四类停止原因、未标定/坏输入/非法 Unicode/巨大区域不建库、parse 三态日志、聚合/停因成功与内部失败轨迹、日志降级、超大坐标 CLI 固定 JSON、仓储错误三键安全出口、严格参数、同一日志/ correlation 与零设备/网络/进程断言。
- `test_read.py`：M3 五类 `read` CLI fixture、页面分类/unknown 门禁、职位卡规范化与重复身份、职位详情正文/截断、未读行身份与像素后备、阈值/分辨率/损坏/未知格式/显式零角标/像素 count、字段阶梯来源槽位/前级优先/冲突/failed/视觉默认关闭/人工/跨上下文自校验/派生结果篡改/保留证据伪造、会话身份与方向/重复正文与方向控件/稳定消息键/重复 ID 会话未知/后页重复传播/跨屏重叠/新增消息同会话核对/过期、公开解析入口标定/context/合并值类型/profile 校验、不支持标定边界零 pixel 事件/实际像素 started→failed、错页零 pixel 日志、定向核对重放/陈旧/错 profile、ReadResult 分派、M1 仓储拒绝非 UnreadSnapshot、undetermined 日志与门禁。
- `test_screen.py`：M3 mock `subprocess.run` 验证 dump/cat/screencap 参数、`shell=False`、5 秒超时、单次 attempt、证据保存成功与失败 `adb_evidence` 一次 started 与单一终态、before guard 拒绝的 `action_guard` 终态、serial 前置拒绝、共享门禁先于采集与生产解析接线；不运行真实 ADB。
- `test_policy_contract.py`：M4a Envelope/Decision 严格键/类型/词表/回显/证据/nonce 租约参数/2048 字节边界、嵌套重复 JSON 键、H 无规范化与四类摘要无自引用、64 KiB 内容对象 65536/65537、事实交集与重复/冲突拒绝、职位五 intent、subject 次要空字符串、围栏拒绝、披露追加后的码点/控制字符/联系方式/禁谈/事实值严格匹配（小数/范围/单位/地点全文、经验单位必需、普通数量问句不误判、未知单位/数量级受控 conflict）。
- `test_policy_pack.py`：TOML/Markdown 完整 schema、缺失/额外/类型/范围/词表、45 组合唯一覆盖、同档重复/跨档冲突/漏项、2 A/4 B/39 C 统一硬下限、有效限制、通配限制、联系方式与包内事实模式、三 md 语法、未知标题不反射、base/system 选择、唯一 `{base,pack,data}` 内容对象、渲染重派生、preview 案例 schema；测试构造兼容 LF/CRLF。
- `test_policy_store.py`：路径/普通文件/链接/BOM/NUL/大小/CRLF、init/validate/preview/list、编辑语气/分档/披露、坏示例受控四键、snapshot 清单与复制前后源重读、restore 三值/base/清单/快照/目标篡改、坏 profile 库 unknown 拒绝、合成 stopped 正路径、失败清理、四键外层、配置错误四键、链接候选 list、load/validate/render/publish 阶段日志与零设备/网络/子进程、Windows 短暂/持续句柄有界重试、暂存所有权/替换/跨实例/内容摘要拒绝。
- `test_bridge.py`：M4b A/B 真实参数数组子进程成功、固定三角色/无工具无历史、伪造 role 保持 user 数据、启动失败一次重试、重试前时钟/包/事实有效期门禁、提交后 20 秒超时不重试、临时创建/输入写超预算零 Popen、2MiB stdout 上限、慢进程/不读 stdin/大 stderr/短命继承 stdout 有界收尾、输出监测/等待 OSError 受控、认证/额度/断连/坏 JSON/重复键/角色不符/坏回显固定诊断、事实来源复验、包变动与桥前后租约过期、RenderedRequest 篡改先拒绝。
- `test_policy_eval.py`：实际加载当前包、公开套件模拟/真实适配器路径、报告绑定 base/pack/provider/model/config/suite 摘要与 correlation、模拟/失败/零样本/缺套件无凭证、成功凭证、授权拒绝前不启动桥且日志唯一失败终态、编辑包改变请求与摘要、24 行映射完整、公开/回归/留出分桶、失败报告无凭证、留出冻结/访问 intent/中断/写失败/并发/重启重复/样本身份换名与子集拒绝/摘要变化/退役实际 regression 准入/登记失败保留未确认准入但不可运行/新批次/外部 provider 拒绝/无上传合同绑定与缺失/零样本未测、有界套件读取、无关坏 regression 准入不影响 public+holdout、报告原子失败清理、真实 Windows junction 拒绝、job_card 正控制、缺 public suite 日志唯一失败终态、阶段日志与未知桥异常、CLI eval help/echo/未确认与 holdout register/retire。
- `test_profile_facts.py`：M4c profile 事实域/CLI/存储绑定：提案不落库、确认后重读再显示、程序来源 ID、有效期、同键冲突/显式 replace、撤销/遗忘/无值审计、联系方式/控制字符拒绝、值不进日志、设备会话占用期间确认写入 `device_busy`/`device_quarantined` 且提案/`show`/其他 profile 不受影响、gate→profile 原子写保护与跨进程竞态、包白名单待办、pack.profile_id 与 `--profile` 不一致拒绝、目录路径不伪造空结果、不同 profile 不共享、旧合成库不导入与元数据绑定。
- `test_device_gate.py`：M4c 固定 OS 门禁与活动检查：路径不随项目根/profile 改变、不同 profile/两逻辑项目根跨进程互斥、完整 owner/pid/host/nonce/profile/路径篡改均不删除占用、session_identity 单次已核行返回且 nonce 篡改拒绝、未知版本/额外对象/旧占用表不改写、崩溃/PID/TTL 不释放、异常隔离、库不可读、非 Windows 拒绝、只读 `device gate inspect` 的 free/held/quarantined/错误状态与不建库、写保护映射、profile 库 leased/running/blocked 状态、受控 `recover` 实际正反例、真实 SQLite `BEGIN EXCLUSIVE` 下 inspect/verify/release/session_identity/recover/activity 只抛既有 DeviceGateError 且锁释放后同一身份复核/释放。
- `test_run_guard.py`：M4c `run check`：read_only 本轮确认、draft 当前验收、A 档固定拒绝、预检零子进程、跨库故障隔离与门禁/profile 打开日志 started/终态配对；limitations 只声明真实设备动作入口/M6 调度/M8 run 未交付，不再声称 M5 动作未实现。
- `test_profile_session_lease.py`：M4c 真实 profile 租约：合法 profile 库经唯一 SqliteStore 领取/续租/失效、`ProfileSessionLease` 与 decide 桥前后拒绝、跨门禁 profile/原库/owner、复制库且参数指向副本、任务两行 owner 同改、门禁 nonce 桥前/桥中变化、错误任务 key/app、任务 owner 篡改与错误 profile ID。
- `test_m4c_closed_loop.py`：M4c 闭环：真实活动 restore、目录路径不发布、三值一致、同 profile 事实/任务审计不重置、旧凭证摘要变化拒绝、唯一仓储快照重核、实际受控合成桥 CLI eval→snapshot→restore→validate/preview→eval 串行。
- `test_action_domain.py`：M5 动作键精确规则、目标/动作绑定、观察→三态证据、前后快照/目标已存在/简历请求与交付/联系方式/删除锚点/列表完整/保护项、apply before 未完成→after 完成才 applied/既有 done 不得 performed、坏观察、现场与 IME 未收敛 pause、授权种类/摘要/期限/确认、人工卡。
- `test_action_store.py`：M5 唯一仓储边界：pending 先落库、动作键-目标错配、leased 未 start 零准入、三态与设备处置、执行后失效冻结、合法真实续租后旧捕获 Lease 完成、事件/任务/设备故障回滚、过期租约、崩溃 pending、旧证据时间、UTC 跨日/额度/冷却/调低/重启、confirm/abandon/取消后受控 confirm、IME restored 账本、异 host/父子命令检查器、命令族账本缺失/损坏/枚举失败 unknown 与 v1→v2 备份迁移；错 profile/坏库/普通 M1 库拒绝且零备份，既有备份保留、合法迁移保留事实。
- `test_action_usecase.py`：M5 生产用例：默认动态 clock、实际 store/profile/gate 绑定、验收四字段缺失/漂移、结构化身份缺失/冲突/调用键与持久任务不符在 pending 前拒绝、公共+真实 ADB 适配器目标职位不符不可逆 tap0、首次 IME 前绑定动作上下文并记录命令族、命令族未证明全停/完成写失败/空 IME 早退时保留已证效果但 unknown/`gate_converged=false`/人工卡、一次真实 complete 写失败准确 hit1 并留下 `action_finish_command` started→failed 安全终态且不降级、任务身份 SELECT 读故障 started→单一失败终态且零设备/pending、leased 未 start 零设备调用、执行中/提交前包/共享身份漂移冻结 unknown、unknown 单次调用且不重放、performed 不改事实、同动作键新任务拒绝、完整七字段证据、坏观察 unknown、持久 IME 绑定/错误原值不收敛/正常恢复、原命令停止恢复门禁、成对 safe 日志与单终态；无账本（含 IME 预读）零子进程、真实合成子进程+真实 Job 账本 performed/done 正例、证据索引 before/after 失败的 tap 数与三态/任务/设备处置。
- `test_action_executor.py`：M5 ADB 原语参数数组/`shell=False`/5 秒/不重试/每命令门禁复核、唯一姓名+完整身份/唯一目标 editor/真实列表容器整层覆盖（可见后代 text/content-desc 缺失/坏/零面积 bounds、列表内中间与更外层裁剪/滚动三阶段拒绝）/唯一类型槽位/目标子树差分、嵌套槽位单一定位、同分组两个同类型槽位歧义零点击、动作后按钮 disabled 仍证明本类型新状态、另一类型已处理不污染、dialog 同类型槽位状态重核、单 tap 拒绝、既有/无关提示不算新变化、点击前后失败事实、IME 前置后切换/早退恢复/读回、固定命令标签、复用 screen 证据+动作索引、真实 Popen 回调记录命令族、平台 Job 有界未证明/空族/不可用三态、执行起点前未闭合事实中断保留 unknown、assign/resume 失败不执行、二次超时/OSError 有界收尾、真实自有合成 Python 子孙 Job 正常停止、身份写失败/超时有界回收、无账本/未绑定/错绑定与绑定后 IME 预读零命令拒绝、证据索引失败 clicked/phase 收敛、坏 XML 恢复拒绝与合成执行器严格观察、apply 原始 XML 控件发现/控件中心点击/无控件零 tap/before 已 done 0 tap/已点击无新鲜完成 unknown。
- `test_action_cli.py`：M5 实际 CLI 组合根：五动作正例、read_only/未确认/未授权/包漂移拒绝、缺身份拒绝、not_performed 与未收敛、unknown 不重放、资格正例/同动作历史/冷却负例、confirm/abandon 对账、设备现场对账坏 XML 拒绝后再正例、人工卡与 restore unknown；read_only reconcile 在 profile/门禁/设备写入前拒绝且状态不变。
- `test_runtime_domain.py`：M6 纯规则：安全点七条件逐个为假、默认预算与等号停止/在途超支、游标往返/重排/插入/消失/重复访问、固定优先级、未知未读一次核对、第 6 次抢占、首次观察保留与单调等待、检测间隔与双时钟漂移。
- `test_runtime_store.py`：M6 仓储：检查点事件故障回滚、`apply_job` performed/applied、unknown 隔离与不重放、typed `job_task_key` 数据库约束、普通 M1 库 job-key 租约在任何 SQL 前 `invalid_input`、v2→v3 备份迁移保留旧动作行。
- `test_runtime_execute.py`：M6 实际运行（合成）：经真实 M3 fixture/门禁与唯一 decide 的职位→未读抢占、实际 M2 解析+upsert 的 queued new_message 在职位前处理且同会话未读优先、hint-only jobs/new_messages/unread_unknown 不产生候选/任务/暂停、read_only 实际计数与零写、七安全点独立证据、实际 unknown 未读只核对一次后暂停恢复、实际读取预算/负超支/重启不回补、缓存证据身份/一致损坏/事实撤销、needs_human/非法 intent 不标 done、completed/queued 批次 restore 边界、缺本轮确认零外部工作、prepare 共享排他、apply 配额缺失零写入、坏路径。
- `test_runtime_g123.py`：M6 G1/G2/G3 生产边界（真实临时 profile/门禁/SqliteStore + 实际 M3 解析 + 唯一 decide）：空 checkpoint 真实派生与真实提交、检查点事件故障回滚、A/B 最近动作不互相洗白/阻断、空/非法身份与尚未 start 的区分、真实 pending 与已提交分离、已收敛历史不阻断新轮；实际一次 `RuntimeReadPort` 定向采集的清除/计数/耗时/日志、旧 hint 反控、全覆盖 absent、viewport/count-only、过期/未来、类型不符、普通读取耗时先结算且耗尽预算不进入定向；首次观察取真实采集完成时刻（250ms 采集→10250 起算）；S01 安全点 20020→实际开始 20200/wait 10200 与安全点前零切换、S08 三窗（含原始可见性切换）arrival/observation/start 分离、未 start 零 `stage_preempted`/零抢占、安全点后漂移与预算耗尽阻断 claim；S05 两卡游标连续刷新/重排不重复；S06 真实 ADB 命令计数与 199/200/201/250 判据尚未完成；S07 1001ms 正常 finish、20/21 秒最新 Lease renew+finish、25 秒 bridge 后 done、start 后 nonce 变化零详情读取。只使用合成原始刺激/时钟，不返回业务答案，不连接设备/provider。
- `test_runtime_apply.py`：`perform_action` + 真实临时 `CommandIdentityStore`/执行器 wrapper 的 apply_job E2E：pre-command 拒绝零命令、performed/applied、after-click unknown/隔离不重放、本地 complete 失败 pending 且重开不重放、真实 `AdbActionExecutor` 原始 XML/合成 adb 子进程控件发现。
- `test_operator_docs.py`：操作手册存在/M6 命令/限制声明与 `operator_docs_report` 缺失/不完整时 BLOCKED，只是文档证据。
- `runtime_helpers.py`：M6 临时项目、M3 read fixture 复制、动态合成发现/会话夹具、场景/决策刺激、CLI 与只读行数辅助；`make_job_fixture` 现生成单卡公开 `job_list` 原始刺激以满足“回列表”判定，完整职位详情边界由专门负控保留。
- `tests/scenarios/`、`tests/test_offline_scenarios.py` 与 `tests/private_regression/` 由独立场景协调端维护；本目录不修改、不把其输出当作本 Task 判据。
- `action_helpers.py`：M5 测试共用的临时项目根、策略包/验收凭证、profile 匹配 seed、动作/对账脚本、合成观察与 CLI 运行辅助。
- `m1_helpers.py`：M1 测试专用的假时钟、合成快照/证据、固定合成 UUID owner/本机 host 摘要、临时库打开与只读行/事件查询辅助；不连接设备。
- `policy_helpers.py`：M4a/M4b 测试共用仓库原文读取、临时项目根（含公开套件）、CLI 运行、合成包写入与有效 envelope/decision/租约截止构造；不调用模型、设备或网络。
- `fixtures/`：M0/M2/M3/M4a/M4b/M5 纯合成输入夹具；见 `fixtures/AGENTS.md`。M4b 桥桩含成功、错误、慢进程、2MiB stdout、大 stderr、不读 stdin 与短命继承 stdout 模式；M5 `fixtures/actions/` 只含合成观察脚本。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、范围边界

- 独立场景协调端维护 `tests/scenarios/` 与 `tests/test_offline_scenarios.py`，私有回归在 `tests/private_regression/`；本目录指南不为这些位置虚构内容，也不得由本 Task 创建或修改。
- 负例由测试进程捕获预期退出码；验收命令本身必须退出 `0`。
- 真实适配器只在测试中 mock `subprocess.run` 与 ctypes；不得实际运行 ADB 或检查真实 PID。M4b 只允许启动 `tests/fixtures/bridge/bridge_stub.py` 这个合成本机进程，不调用真实模型或网络。M4c 共享门禁与 profile 库只用临时项目根/临时 OS 根/合成事实/跨进程模拟，不写真实 `LOCALAPPDATA` 或读取旧本机库。`sense`/`read`/`policy` 测试额外 mock/断言零设备、网络、子进程与任务租约写入口；`policy` 用 `FilePolicyRepository` 在临时项目根运行，不写仓库 `prompts/local`；`policy eval` 产物只写临时项目根的 `data/evals`。
- 测试不把被测输出反推成预期，也不使用真实姓名、序列号、聊天、数据库或绝对路径；`fixtures/sensor/expected.json`、`fixtures/read/expected.json` 只由测试读取，`fixtures/policy-preview`/`evals/policy-suite` 不含模型答案或参考输出。M4b 合成决策表由测试独立构造，不从断言侧或旧 24 参考输出回填。
- 日志测试覆盖正常追加、白名单拒绝、IO 故障注入与 CLI 固定 `m0_process_log_degraded` 标记；fixture 模式只允许写被忽略日志，M1/M2 只写临时库。每次运行日志必须独立，显式路径也不得退回共享追加；CLI 默认生产日志保留在被忽略目录，测试不得按目录差集删除文件。
- M1/M2/M3/M4a/M4b 反例必须显式断言失败原因与“任务/设备/事件全部回滚”“未创建业务库”“未生成可发送身份/未通过门禁”“未留下半包/未覆盖旧包”或“未启动桥/无验收凭证”，不能只断言抛出异常。
- M4a 生产 `policy restore` 已接真实活动检查；测试用坏 profile 库/共享占用覆盖 active/unknown 拒绝，正路径用临时 stopped 检查，不新增公开 CLI 绕过参数。M4b 模拟 transport 必须仍经过同一授权回调，且 `simulated=true` 永不写验收凭证；真实适配器测试的评测报告也强制 simulated 标记。留出测试只使用临时项目根合成套件/登记/合同，不读取真实私有数据；跨进程/线程写登记使用同一锁，不按目录差集清理证据。

## 四、运行方式

在项目根执行：

```powershell
python -m unittest discover -s tests -p "test_*.py"
npm test
```

M0–M4b Python 测试不依赖 Node；`npm test` 验证既有 24 案例判据未被破坏。

## 五、维护要求

1. 新增/改名/移动测试或夹具目录时同步更新本文件和根 `AGENTS.md`。
2. 新增测试模块时保持标准库优先，失败信息不回显敏感原文。
3. 每个测试方法写 docstring，说明覆盖的契约与预期。
4. 新增真实设备测试必须另行取得当轮确认，不能混入离线测试。
5. 修改 M1/M2 契约时同步更新 `m1_helpers.py` 或合成夹具，但辅助函数不得替被测代码生成期望结果之外的状态。

## 六、已知限制

- 当前覆盖 M0–M6；M7 清理、M8 完整 run/灰度、真实 provider/真机/真实 OS 门禁目录与真实动作执行测试尚未实现。M6 场景只含刺激，不含 expected/oracle；测试协调端维护的 `tests/scenarios`、`tests/test_offline_scenarios.py` 与 `tests/private_regression/` 独立判据不属于本目录维护范围。
- 真实 ADB、真实机型、窗口/截图输出与真实消息稳定标识未验证；M1 停止检查器只用注入值或 mock ctypes；M2 只用合成 XML/profile；M3 只用合成 XML/RGBA 与 mock subprocess；M4a 只用临时项目根、合成 base/包/preview 与真实 stopped 检查；M4b 只用合成本机桥桩/模拟 transport；M4c 只用临时项目/OS 根、合成事实/凭证/桥桩与跨进程模拟；M5 只用合成动作脚本/mock ADB/临时 profile 库，真实 provider、私有留出、真实租约、真实 OS 门禁目录、真机动作与恢复时序未测。
- 独立离线场景与私有回归测试不在本 Task 范围。
- 日志测试验证的是本机 best-effort 行为，不代表磁盘故障时仍能保留完整日志。
