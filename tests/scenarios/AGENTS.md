# tests/scenarios 目录指南

## 一、目录用途

本目录存放 BOSS 直聘离线场景测试资产：按《BOSS直聘_求职自动化_场景测试方案_20261123》登记的 73 个场景及其全部变体、独立 expected、纯合成夹具、标准库执行辅助、使用说明与三轮作者自审记录。它服务于“测试资产/判据自检”和“对真实交付 app 的离线场景验收”两条不同链路，不实现未读解析、调度、动作执行或任何业务替代实现。

M0 `51333d8`、M1 `a600d0a`、M2 `cc103f8`、M3 `584b814`（普通 merge `92a0b68`）、M4a/b `140c182`（merge `ec66db4`）、M4c `5cb6817`（普通 merge `248483e`）、M5 冻结 `9f94a46`（普通 merge `e02b9f9`）已接入并在隔离框架实际执行；M6 为有限接线：A06 六变体、S03、S01 两变体、S02/all_true 与 S08 已 READY 并定向执行，S02/single_false_blocks 与 S04 两变体仍 BLOCKED，S05–S07 仍 BLOCKED，M6 全量/真机/模型未验收。READY 表示入口、夹具和独立预期齐备，实际执行仍受里程碑门禁限制；ASSET_ONLY 接口未冻结；DEFERRED 与 MODULE_EVIDENCE 不计 PASS；T/O/W 实机门槛保持未验收。M0 29 PASS、M1 38 PASS、M2 54 PASS、M3 15 PASS、M4 77 PASS、M5 43 PASS 为历史执行记录，不等于产品整体通过。

## 二、文件与子目录索引

- `catalogue.json`：73 个场景、364 个变体的机器清单，含 milestone、源契约、前置、离线子场景、SUT 入口步骤、夹具引用、覆盖状态和 `expected_ref`。
- `expected/`：独立 expected 与目录指南。每个变体一条判据，只由比较器和审查者读取；夹具与 manifest 不引用该目录。`expected/m6-observations.json` 保持原有 S01/S03/S06/S08 观测 raw 字节不变；新增 S01/arrival_after_commit 等观测写入独立 `expected/m6-supplemental-observations.json`，由测试 comparator 接入，不进入 SUT。
- `fixtures/`：纯合成输入与目录指南。包含 M0 doctor/config、M2 sensor、M3–M8 事件/动作/策略/清理/指标声明式输入；不含真实联系人、公司、简历、设备标识或本机路径。
- `framework/`：标准库辅助与目录指南。包含资产校验、严格比较器、虚拟时钟/事件重放、隔离验收入口。
- `tools/`：作者生成脚本与目录指南。`build_assets.py` 按人工判据机械展开清单、预期与夹具，可重复生成；不读取产品输出。
- `reviews/`：三轮作者自审记录与目录指南。明确是作者自审，不是独立第三方审查或实机验收。
- `README.md`：面向执行者的使用说明、命令、退出码、层间边界与已知限制。
- `AGENTS.md`：本文件，目录索引与维护要求。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、当前规模与覆盖口径

最近一次生成与自检结果（以实际命令输出为准）：

- 场景 73 个：M0 3、M1 7、M2 15、M3 14、M4 11、M5 8、M6 8、M7 4、M8 3。
- 变体 364 个：F 342、T 9、O 12、W 1；M4 103 个（66 原 + 37 显式子证据）。
- 资产状态：READY_FOR_APP 267、ASSET_ONLY 35、DEFERRED 35、MODULE_EVIDENCE 5、DEVICE_GATE 22。READY 267 是资产接线状态，不是 267 个实际 PASS；旧全量 256 PASS 是历史记录，S05–S07 仍 ASSET_ONLY/BLOCKED。
- M0 已执行：C01 9 PASS + 1 T 门槛、C02 12 PASS、C03 8 PASS；共 29 PASS、0 FAIL。C01 real_probe 仍是未验收 T 门槛。
- M1 已执行：38 PASS、0 FAIL；2 DEFERRED（M5/M6）、3 MODULE_EVIDENCE（纯约束，不重做模块单测）。M1 总体仍 BLOCKED，不冒充完整通过。
- M3 已执行：15 PASS、0 FAIL；8 DEFERRED（R01 三项、R02、R03/R04/R06/R07 原变体）、1 MODULE_EVIDENCE（R01 无确认的零子进程证据）、17 设备门槛。新增 10 个明确父场景的 M3 子变体（定向核对 API/边界、全局正计数 API、读侧不关闭发现、稳定身份新增键、页面/职位完整路径）；它们不计作原 R03/R04/R06/R07 完整通过。
- D11 留出公开/冻结/首次失败/冲突/IO/无提供方/未知案例等 11 变体由真实探针执行；所需无上传合同以显式公开合成文件名 `fixtures/m4/eval/attestation/holdout-attestation.synthetic.json` 交付，变体用 `fixture_files` 映射到隔离根的固定 `data/evals/holdout-attestation.local.json`，不依赖被忽略的本机文件。
- M4a/b/c 已执行：77 PASS、0 FAIL；25 DEFERRED（D02 原从新租约重跑、D03 原超限待办、D04 原 ask_user/待办/授权、D06 完整十步、D09 read_only 整轮、D10 额度/旧授权/在途）、1 MODULE_EVIDENCE（operator_docs）。D01 真实映射 24 行、D02 12+真实租约适配、D03 2+真实租约桥前后拒绝、D05 15（真实 junction、16/32/64 KiB 等/超边界、坏 UTF8/BOM/NUL）、D06 2、D07 3 子证据（Local/External、hr/job/anomaly）、D08 5（独立手算摘要、2048/2049、65536/65537）、D09 7（两进程恰一 primary、失败隔离 quarantined、崩溃/TTL/PID/换 profile/字段篡改、预检先占后开）、D10 7（真实活动 stopped/active/unknown、真实 restore 三值/目标不半写、改 base/包/model 旧凭证失效）、D11 11；M4c 事实过期/冲突/白名单外/跨 profile/HR 键与真实活动/租约子证据均不冒充完整 M5 流程。
- M5 已执行（历史记录）：43 PASS、0 FAIL；A01 6、A02 8、A03 7、A04 7、A05 6、A07 3、A08 6。旧记录中的 11 个 BLOCKED_ENTRY_UNFROZEN 含 A06 六个依赖 M6 apply_job；A06 六变体现已在 M6 有限接线中 READY（见下条），旧 M5 子集结果不改为新 PASS。A01 输入法/发送控件候选缺 M5 控件发现；A03/A04 direct_send 被生产执行器前置拒绝（非文本需先有可逆槽位点击）；A07 reconnect 缺 M5 键屏/会话身份入口。M5 子集退出 3，不冒充产品通过。
- M6 有限接线（本候选）：A06 六变体由 `framework/m5_probe.py` 的 `perform_action(apply_job)` + 显式合成配额/真实执行器/唯一仓储/job 租约执行；S03 由 `framework/m6_probe.py` 消费原始 `virtual_ms/arrival_at`/安全点顺序并驱动真实 `execute_run`，arrival 作为实际执行边界页步骤进入 EnvironmentClock 轨迹，unsafe 前后由真实 scheduler_events/持久 cursor 采样；S03 的 25 步由原 19 个 job/snapshot/unsafe/safe 边界，加上每周期一个、合计六个 arrival 页边界构成；25 是执行边界数，不是业务动作数或 PASS 数。S01 两变体由原始 job/arrival/snapshot/safe 刺激驱动真实 `execute_run`，安全点后 180ms 检测切页是独立读取步骤，first_observed/handling/waited 从持久 `stage_preempted` 与真实 `observe_unread` 轨迹读取；arrival_after_commit 只在下一次正常检查记录 first_observed。S02/all_true 用真实 `begin_action`/`freeze_attempted_action`/`reconcile_action` 制造并收敛 pending/unknown，再由真实 `execute_run` 全真切换，并输出切换前真实 M3 读取 status/actionable、空输入、无确认框、无在途、无 pending、最近动作 `not_performed` 与同批次 checkpoint/stage 顺序；变体原 `fixture` 保持 `fixtures/m6/safe-point/vector.json`，新 `converged-retry.stimuli.json` 只由 steps/shared_inputs 提供；未决现场保留 held 门禁并以 `device_busy`/零读取拒绝。S08 三个时窗由真实 `observe_unread` 调用与 `stage_preempted` 读取 0/1000/null；第三到达由驱动 inbox 在 1900ms 接收外部合成载荷（at_ms/会话键/载荷 SHA256），不预置产品发现记录，2000 刷新按 inbox 与可见性选择真实 M3 读取源；默认不可见时产品队列 absent、零观察，可见变异才读取真实 unread XML 并产生 2000 观察，缺失到达/不可见时原判据仍拒绝。S02/single_false_blocks 仍 BLOCKED：pending/unknown 前置拒绝与正常 bootstrap checkpoint 属真实保护，`_safe_scene` 的 list_returned 对 `chat_detail/job_detail/main` 仍为真（由真实 M3 解析与真实 `_safe_scene` 观察记录，未改生产）；S04 两变体仍 BLOCKED：`execute_run` 只消费 `step.unread_verification`，未调用真实 `targeted_verification_outcome`。A06 六 + S03 + S01 两变体 + S02/all_true + S08 定向可执行。S05–S07 仍 BLOCKED（生产合同未交付）；M6 全量、真机、真实 provider 与 merged-main 未验收。
- 其余 35 个 M5+ ASSET_ONLY 记 `BLOCKED_ENTRY_UNFROZEN`；DEFERRED/MODULE_EVIDENCE 均不得算 PASS。S05–S07 仍在该 BLOCKED 责任内。
- DEVICE_GATE 是 T/O/W 纯实机门槛，保留未验收；F 通过不能替代。
- 日志判据绑定单次调用产生的唯一日志文件与 correlation，校验完整有序 `stage/operation/result/reason_code` 序列、`started` 成对次数、固定字段白名单/类型和敏感标记；无像素分支不得出现 pixel。比较器对 rows/cards/messages/field_evidence 的每个元素校验固定键与必要类型，并对非首项身份/摘要/证据给出独立预期。

## 四、入口与验证命令

在项目根执行：

```powershell
python -m unittest discover -s tests -p test_offline_scenarios.py
python -m tests.scenarios.framework.runner --mode assets
python -m tests.scenarios.framework.runner --mode acceptance --target .
python -m tests.scenarios.framework.runner --mode acceptance --only SC-C01 --target .
```

- unittest：资产完整性、严格比较、逐元素键/类型、时钟/重放、路径安全、隔离 tripwire、缺 app 拒绝、M3 探针/真实阶梯 mutant、真实日志变异、非首项输出变异、M4 提交后重试/桥工具泄露/模拟验收/常量摘要/首次失败收尾/regression 篡改 mutant、M4c 探针正常执行/越界与 junction 拒绝/生产字段变异被 oracle 拒绝/D06 forget 残留/并发 winner 关联/隔离 LF 边界（含 BOM/NUL 原样）/单文件夹具映射与根内绝对/上跳/Windows 语义/答案名拒绝/完整 prepare 字节矩阵（自动与显式 LF）/资产校验同规则/合成无上传合同正例与缺合同零桥调用反例/受控异常安全 JSON、M5 原始 JSONL 与生产格式账本变异（缺 begin/complete、多文件、坏行、错序、删 finish、错分类、非法字段、敏感载荷、逐命令实例缺/重/乱序/跨命令/同 label 实例）、合成 Popen 计数正增量/异常/零调用、实际 `_gate_race` 持续 None/错身份输出送原 D09 关系、M6 S01/S02/S08 真实探针正例与原始刺激变异负控（`M6SevenVariantTests` 8 项），以及 6 个真实 facts CLI 持久/held/quarantined/unknown/busy 拒写案例等 161 项检查（含新增 M6 七变体对比与缺口探针；M5/M6 探针只在 acceptance 或定向测试运行）；只证明判据能抓错误。
- `--mode assets`：只读清单/预期/夹具并校验；退出 0 仅表示资产自洽，不表示产品通过。
- `--mode acceptance`：先校验资产，失败即拒绝执行；只为已实现里程碑的 READY F 变体创建隔离根，运行真实 `python -m app`、M0 协议边界替身、M1 探针/受控两进程、M2 受限探针、M3 探针、M4 探针或 M4c profile/门禁/租约探针/CLI。旧全量 256 PASS、0 FAIL 是历史记录；当前 READY 267，A06 六 + S03 + S01 两变体 + S02/all_true + S08 定向可执行，S02/single_false_blocks 与 S04 两变体仍 BLOCKED，S05–S07 仍 BLOCKED，全量未在本轮重跑。M5+ ASSET_ONLY/DEFERRED/MODULE/设备门槛使总体 BLOCKED、退出 3；M0 子集退出 0，M1 子集退出 3，M2 子集退出 0，M3 子集退出 3，M4 子集退出 3，M5 子集退出 3，均不声明产品 PASS。
- 报告 JSON 写入被忽略的 `data/evals/scenario-runs/`；不提交生成报告，不修改 `.gitignore`。

## 五、维护要求

1. 修改场景、变体或预期时，先改 `tools/build_assets.py` 中的人工判据，再运行生成脚本；不要手工编辑生成 JSON 后与作者脚本脱节。
2. 新增 READY 变体必须有冻结入口步骤、夹具和可执行 acceptance；只写文字 assertions 的变体只能是 ASSET_ONLY，不得标 READY。
3. 每个变体必须有 `expected_ref` 且独立预期只存在于 `expected/oracles.json`；夹具与 manifest 不得引用 expected。
4. 不读取 `config.json`、环境凭据、真实 HR 内容、历史业务库或截图；夹具只用“合成联系人甲/合成机构甲/合成岗位甲”等合成值。
5. 不复制模块单元测试；这里只做跨模块场景与横向不变量。发现生产缺陷先向协调端报告，由业务方修复。
6. 新增、删除、改名或移动本目录文件/子目录时，同步更新本文件和根 `tests/AGENTS.md`（由业务方串行维护，本任务不直接修改）。
7. 三层验收分开报告：资产自检、已交付里程碑的真实 CLI 场景、未实现/纯实机门槛；缺 app 或未执行不得写成 PASS。
