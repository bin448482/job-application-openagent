# app/usecases 目录指南

## 一、目录用途

组织单次业务用例的输入校验与领域判定，不直接访问属性、文件、数据库或网络。当前包含 M0 `doctor`、M2 `sense` 的持久化/聚合结果编排、M3 `read` 的决策门禁/定向核对/JSON 组装、M4a `policy` 的初始化/校验/预览/列表/快照/恢复编排、M4b 单次决策桥编排与 `policy eval` 分层评测编排、M4c 事实确认、验收复核与 `run check` 预检编排，以及 M5 单次动作执行、文本资格与人工对账编排。

## 二、当前文件

- `__init__.py`：声明用例包。
- `doctor.py`：严格校验合成探针输入（含 UTF-8 可编码性），按“清单 → 选择 → 状态 → 解锁 → 前台”的顺序判定；`build_result` 是唯一 doctor 业务结果组装点，CLI 与设备失败共用它。
- `sense.py`：`persist_and_aggregate(store, result, process_log=None)` 先 upsert 快照、再查未关闭会话，调用领域 `aggregate_unread` 与 `stop_reason_for`；两步分别以 `sense/aggregate`、`sense/stop_reason` 记 started→ok/failed（内部异常保留 `error_category=internal` 并继续公共安全出口），不记录正文/身份/路径/异常消息。`build_sense_payload`/`result_payload` 是 M2 唯一 JSON 结果组装点，不读取 XML 或数据库。SQLite 初始化与打开仍由 CLI 在解析/合并 ok 后执行。
- `read.py`：`is_actionable(result)`/`require_actionable` 按值类型检查页面、职位、未读与会话；结构 ok 但字段未知/身份未决仍拒绝决策。`build_read_payload` 是 M3 唯一 JSON 组装点；`targeted_verification_outcome(initial,targeted,queued_keys,*,now)` 要求新 snapshot、同 app/profile、observed_at 在 0–5 秒且不老于 initial，重放/陈旧/跨 profile 分别返回 `unread_unverified/stale_snapshot/identity_ambiguous`；`read_from_device` 在任何门禁或采集前拒绝不可调用 parser，再要求共享设备门禁通过。不调用仓储、模型或动作。
- `payload.py`：M2/M3 共用的 `ReadResult` 七键与 `UnreadSnapshot` 深层序列化唯一实现；用显式字段投影而不 `asdict`，保持 M2 `SIGNAL_KEYS` 顺序与全部字段，`sense`/`read` 均复用。
- `policy.py`：`init_pack/validate_pack/preview_pack/list_packs/snapshot_pack/restore_pack` 只经 `PolicyRepositoryPort` 做文件事务，经领域纯函数校验/摘要/渲染；`init` 只建 local 直接子目录并保留 profile 身份；`snapshot` 复制前后重读活动源且清单完成才发布；`restore` 要求清单/快照/目标三值 `pack_hash` 相等、base_hash 与 base_version 匹配、保留 profile_id，staging 同卷发布且不覆盖。`load_active_pack` 是 eval 复用的当前包加载/校验入口。所有用例可选注入同一 `ProcessLog`，在 load/validate/render/publish 实际边界记录 started→ok/failed/undetermined 与耗时。`RefusingPolicyActivity` 保留为测试/受控拒绝实现；M4c CLI 注入真实 `ProfilePolicyActivity`，active/unknown/坏库/未决均拒绝，只有 stopped 允许恢复。
- `decision.py`：`decide(...)` 调用既有 `verify_rendered_request`、`parse_decision`、`validate_decision`、`append_and_validate_reply` 与 `resolve_policy_tier`，不复制契约。桥前、重试前与桥后重读可信时钟、租约和当前 base/pack；重试前重新解析当前包并用唯一 `verify_rendered_request` 复核事实有效期/回显；桥期间跨截止、事实到期、包变化、坏回显/坏证据/披露或事实失败均丢弃输出；只有传输层证明进程未启动的 `bridge_start_failed` 重试一次，提交后超时映射 `decision_timeout` 不重发。未知桥异常映射 `bridge_protocol` 并写终态日志。返回 Decision、复验回复、最终档位与不含正文的审计摘要（attempts/submissions）。
- `facts.py`：M4c 事实提案/确认/再显示/撤销/遗忘编排；未确认只返回提案，确认后从库重读并显示值，同键多来源转 conflict 待办，`--replace` 才显式作废旧来源；撤销保留历史，遗忘只保留来源 ID/无值审计；不把值写入日志/错误。
- `acceptance.py`：M4c 只读读取本机 `data/evals/eval-<id>/acceptance.json`，比较当前 base/pack 与可选 provider/model/model_config_hash；缺失凭证 `authorization_required`，摘要/身份变化 `policy_changed`，不授予设备或发送权限。
- `run.py`：M4c `check_preflight` 只在 read_only 本轮确认、draft 当前验收（provider/model/model_config 必须齐全）、A 档固定拒绝下做预检；先取得共享占用再打开 profile 库，跨库失败保留占用并隔离，profile 打开记录 started/终态；不探测设备、不调用模型、不执行动作、不预建 M5 表。M5 起 profile 的 `pending/unknown/cancelled` 动作也视为未决，阻止预检通过；成功 `limitations` 只声明真实设备动作入口未交付、M6 调度与 M8 run 未实现、设备探针未运行，不再声称 M5 动作未实现，输出仍为字符串列表。
- `actions.py`：`perform_action` 默认使用动态 `utc_now`，先重核实际 store/profile/路径、running 租约、共享身份与当前 base/pack/验收四摘要；结构化 `ActionIdentity` 的姓名/公司/职位重算会话键必须与持久任务一致，缺失/缩减/冲突在 validate 阶段 `precondition_failed` 且执行器零调用。随后先原子 `begin_action`（pending/额度/键锁），再在首次原 IME 读取前绑定动作上下文；受控 IME 读取、执行与共享采集都归入同一动作命令族。绑定后的 IME 预读要求非空命令账本，缺失时在零设备命令处拒绝；真实适配器 `execute` 在无账本/未绑定/错绑定时同样零命令拒绝，生产用例无账本只能保留 pending/blocked、`gate_converged=false` 与隔离。任一命令未由平台/枚举证明全停、完成写失败或空 IME 早退时，执行器返回 `command_quiet=false`，现有 pause/complete 逻辑保留已证效果但记录 unknown、`gate_converged=false`、生成人工卡并保留共享隔离；唯一账本闭合步骤的真实落盘失败另记 `action_finish_command` started→failed 固定安全终态。证据索引失败按真实点击与阶段保守收敛：before/readback/dialog 不固定 clicked=True；readback 已输入，不按 idle 推断输入为空/IME 已恢复，也不把在途命令事实当作已停；输入/回读异常保留底层未证明命令族，不硬写 scene_settled/command_quiet；点击后仍为 unknown 且不重试。执行后 guard/租约/共享异常统一按原身份冻结 `unknown/blocked` 并保留隔离；合法真实续租由仓储当前截止支持。坏观察或执行中漂移收敛 unknown，不重试。`evaluate_text_eligibility` 复用持久化动作历史与额度/冷却。`reconcile_action` 核对实际 store/profile、持久化原 IME、原 host、本轮显式确认与原命令停止检查器；CLI 会写业务终态的 reconcile 固定只允许 `draft_only`，`read_only` 在打开/迁移 profile、门禁或设备前拒绝；abandon 保留锁且之后仍可受控 confirm 而不重开任务。显式 `ProcessLog` 记录 validate/admit/execute/derive/commit 成对阶段。
- `policy_eval.py`：`eval_pack` 实际加载当前包与公开/回归/留出套件，经同一 `decide`/`DecisionBridgePort` 逐例运行；每例使用 case_now+真实单调耗时的逻辑时钟；缺套件/零样本返回 not_measured 且零授权零调用；authorize 返回 false 在同一阶段记 started→failed；报告绑定 base/pack/profile/provider/model/model_config_hash/suite_hash 与 correlation_id，逐例只留 case_index/摘要/固定诊断；只有公开集全通过且非 simulated 才写验收凭证。留出在任何调用前锁内持久化 intent，完成/失败/中断/重复如实收尾；无上传合同必须绑定当前 model_id/model_config_hash 且所有禁用项为真；`register_holdout`/`retire_holdout` 维护样本身份占用、实际 regression 准入副本与整批退役；退役登记写失败保留未确认准入本机残留，由 registry 资格过滤使其不可运行，不删除其它 owner 文件。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、行为契约

- 输入必须是恰好包含 `devices/selected_alias/unlocked/foreground_app` 的对象；设备项恰好包含 `alias/state`。
- `alias` 唯一、非空且可按 UTF-8 编码；`state` 仅 `device/offline/unauthorized`；`selected_alias` 为已有别名或 `null`，且同样拒绝不可 UTF-8 编码的孤立代理字符；`unlocked` 为布尔或 `null`；`foreground_app` 仅 `boss/other/unknown`。
- 零设备报 `device_disconnected`；多设备未选择报 `device_ambiguous`；单设备未选择时自动选择。
- 选择后依次判定离线/未授权、锁定或解锁未知、前台不是 BOSS 或未知。
- 失败保留已知字段，未知字段保持 `null`/`unknown`；不伪造业务成功。
- 本模块不导入具体适配器，不启动子进程。CLI 的设备失败只调用 `build_result`，不得在 CLI 重复组装七键对象。
- `sense` 不允许伪造成功的 ReadResult：只有解析器已产出 `status=ok` 且合并有效的结果才可调用仓储；仓储错误由 CLI 映射为三键安全失败对象。
- `sense` 不 claim/start/finish、不消耗发现、不推进已读基线；停因来自纯聚合结果，不在这里重写信号或候选。
- 重复 JSON 对象键在 CLI 共享输入边界或解析器输入边界拒绝，不在本模块另造解析层。
- M3 `read` 不让 `undetermined` 或字段未知进入决策；门禁原因固定 `not_ok/field_unknown/identity_ambiguous/incomplete/no_target`。`read` 不写库、不关闭发现、不推进已验证基线；合成 `actionable=true` 不提供发送授权。
- `read_from_device` 必须在任何采集前调用 `DeviceGate.require_available()`；M4 共享门禁未交付时抛出 `DeviceGateUnavailable`，不得回退到 profile 内任务锁或真实子进程。
- M4a `policy` 不读取根 `config.json`、设备、模型或网络；preview 只读固定 `tests/fixtures/policy-preview` 合成案例并纯渲染，不读黄金答案。`init`/`snapshot`/`restore` 的暂存路径以点前缀命名，活动包发现器不接纳；失败只清理本次登记路径，不按共享目录 glob 删除。
- `snapshot_pack` 在暂存写入后、清单发布前及最终发布后重读活动源四载荷与双指南；源字节/身份变化或读取失败以 `policy_changed` 拒绝并清理本次自有发布/暂存，不改变原活动包。`init` 使用同一 `_write_payload`/`_verify_staging`，坏示例返回带定位的 `errors` 列表而不是嵌套异常。
- `restore_pack` 只接受 `prompts/local/history/<snapshot-id>` 与新的 local 直接子目录；M4c CLI 注入真实 `ProfilePolicyActivity`，非 `stopped` 时以 `policy_changed` 拒绝，未知状态不得当已停止；失败不换目录解锁或重置额度，只有 stopped 才恢复。
- `_policy_stage` 在 load/validate/render/publish 实际边界记录同一 `ProcessLog`/correlation 的 started→ok/failed/undetermined、固定原因和耗时；日志失败不改变业务结果；不记录正文、事实、路径或异常原值。
- `decide` 只使用渲染请求的固定三消息与 `protocol` 回显，不把不可信输入变成 role/tool/message；桥返回内容只解析、不执行。桥启动失败可重试一次，重试前重读时钟/租约/包；进程启动后超时/schema/证据/回显失败不重试。所有失败只暴露固定原因码与字段诊断。
- `eval_pack` 每例只把 `input` 投影进 envelope；断言、metadata、答案与其它案例不进入请求。报告/凭证只写 `data/evals`；`simulated` transport 永不写验收凭证。留出只允许 `local-process` 且必须有无上传合同，缺条件零调用；访问意向先持久化，失败/中断不恢复成未看；退役批次实际进入 regression 准入并被同一生产 regression 路径读取。
- `--confirm-external-send` 仅是模型评测外发/计费的本轮确认，不授予设备、事实写入或业务发送权限；生产 CLI 在授权回调返回 false 时不启动桥。
- M5 动作执行只在共享门禁已持有、任务 `running` 且租约有效时开始；`leased` 未 `start`、授权/摘要漂移、额度/冷却/未决文本或现场未收敛均在执行器调用前或提交边界拒绝。动作结果、任务状态与 `action_events`/`events` 在一个事务提交；`unknown`/未收敛 `pending` 保留动作锁并隔离设备，不自动重试。

## 四、测试

```powershell
python -m unittest tests.test_doctor tests.test_sensor tests.test_sensor_store tests.test_read tests.test_screen tests.test_policy_store tests.test_profile_facts tests.test_device_gate tests.test_run_guard tests.test_profile_session_lease tests.test_m4c_closed_loop tests.test_bridge tests.test_policy_eval tests.test_action_domain tests.test_action_usecase tests.test_action_store tests.test_action_cli
```

`tests/test_policy_store.py` 覆盖六命令文件事务、四键外层、坏 profile 库 unknown 拒绝、清单篡改、失败清理、日志阶段与零设备/网络/子进程。`tests/test_profile_facts.py` 覆盖事实提案/确认/冲突/撤销/遗忘/无值审计/值不进日志；`tests/test_device_gate.py` 覆盖共享门禁与 profile 活动检查；`tests/test_run_guard.py` 覆盖确认/验收/A 档/跨库隔离；`tests/test_m4c_closed_loop.py` 覆盖真实活动 restore、目录路径不发布、三值一致、事实/任务审计不重置、凭证失效、快照重核与实际 CLI 评估闭环；`tests/test_profile_session_lease.py` 覆盖唯一 SqliteStore 接入合法 profile 库与真实租约失效。`tests/test_bridge.py` 覆盖 A/B 参数数组子进程、固定三角色、一次启动重试、提交后超时、固定认证/额度/断连/角色/协议诊断与事实复验；`tests/test_policy_eval.py` 覆盖实包加载、报告绑定、分桶、留出冻结/访问/退役、模拟无凭证与 CLI help/授权拒绝。`RepositorySnapshot` 是 eval 与决策测试共用的唯一仓储快照实现，不再保留第二份 PolicyFileSnapshot。

## 五、维护要求

1. 修改选择顺序、原因码、门禁或输出字段时，同步更新根 `AGENTS.md`、`app/cli/AGENTS.md`、`docs/operator.md`、M3 契约文档与测试。
2. 新增用例文件时必须有对应里程碑和测试，不预建空模块。
3. 每个函数写 docstring，说明输入、输出与业务含义。

## 六、已知限制

- 当前只有 `doctor`、`sense`、`read`、M4a `policy`、M4b `policy eval`/`policy holdout`、M4c `facts`/验收复核/`run check` 与 M5 `action perform/eligibility/card/reconcile`；M6 调度尚未创建。`run check` 只是预检，不执行完整 run 或动作。
- 真实设备模式只复用 `doctor` 判定；M2 `sense` 只读合成夹具，M3 `read` 只读合成输入且不提供真实 `--device` 入口；M4a `policy` 只在合成项目根/临时目录验证，生产 `restore` 接真实活动检查，仅 stopped 允许；M4b 只用合成本机桥桩与模拟 transport；M4c `facts`/验收复核/`run check` 只用临时项目/OS 根与合成事实；M5 只用合成执行器、mock ADB 与临时 profile 库，真实 provider、私有留出、真机、真实活动对账与真实发送未接入。
