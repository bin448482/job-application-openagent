# app 目录指南

## 一、目录用途

承载 M0 起的业务程序。当前已实现 `python -m app` 入口、只读 `doctor` 探针、M1 本机状态库（`store init` 和 SQLite 仓储）、M2 合成传感器（`sense` 未读四源解析/入库）、M3 离线读取（`read` 页面/职位/未读/会话、字段阶梯、像素与跨屏身份）、M4a 策略包离线编辑/校验/预览/快照/恢复（`policy`）、M4b A/B 决策桥/公开评测/留出冻结登记（`policy eval`、`policy holdout`）与 M4c profile 独立事实库/本机 `facts`/固定 OS 共享设备门禁/restore 活动检查/验收复核/`run check` 预检；按“CLI 组合根 → usecase → port ← adapter”的依赖方向组织，`domain/` 只放无 IO 模型与校验。M5+ 动作/调度尚未实现。

## 二、当前文件与子目录

- `__init__.py`：声明 `app` 包。
- `__main__.py`：`python -m app` 的进程入口，调用 `app.cli.main.main`。
- `cli/`：参数解析、配置校验、路径边界、JSON 输出与组合根；见 `cli/AGENTS.md`。
- `domain/`：M1 纯领域模型、键/摘要算法、无 IO 校验与 M2 纯聚合/停止原因；见 `domain/AGENTS.md`。
- `ports/`：能力端口与领域快照/租约类型；见 `ports/AGENTS.md`。
- `usecases/`：单次用例的校验、持久化编排、唯一业务结果组装与判定；见 `usecases/AGENTS.md`。
- `adapters/`：具体平台通道；见 `adapters/AGENTS.md`。
- `process_log.py`：每次运行创建独立的固定字段本机 JSON Lines 过程日志，默认写到被忽略的 `data/logs/m0-process-<运行 UUID>.jsonl`；显式路径也只作命名基础，不共享追加。写失败或白名单拒绝都设置降级标记并返回 `False`，不抛出、不打印原值；唯一 `emit_degraded_warning` 每次运行只写一次固定 stderr 标记，CLI 与仓储 API 共用。阶段已扩展到 `store/sense/read/policy/facts/profile/device_gate/run/eval/decision/adb_hierarchy/adb_screen/adb_evidence`，结果含 `started/ok/undetermined/failed`，并以 `operation` 记录 M0–M4c 固定操作（含 eval/verify/bridge/suite/authorize/case/report/acceptance/holdout、facts set/show/revoke/forget、device_gate acquire/verify/release/quarantine、profile open_profile、run check、current_lease_expiry 等），`case_index` 只作安全序号；仍复用唯一日志器。

## 三、依赖与边界

1. `domain/` 只依赖标准库与自身，不导入 `adapters/`、`sqlite3`、`adb` 或具体应用控件。
2. `ports/` 只依赖标准库、自身类型与 `domain/` 模型，不导入 `adapters/`。
3. `usecases/` 依赖 `ports/` 与领域对象，不导入具体适配器。
4. `adapters/` 实现端口，不写调度、不决定策略档位；SQLite 适配器只写本机业务/profile 库与固定 OS 共享门禁，ADB 适配器只做显式探针/采集并复核门禁。
5. `cli/main.py` 是当前唯一组合根；只有它可以把 fixture 输入、ADB 适配器、SQLite 初始化、profile 库或共享门禁注入用例。
6. M0 的 fixture 模式必须零设备、零网络、零子进程；CLI 可以导入 ADB 适配器模块，但导入本身必须零副作用，fixture 分支不得实例化或调用探针。真实 ADB 适配器只能在显式 `--device --serial` 下调用，且只能在测试中 mock，不在离线验收中执行。
7. M1 的 `store init` 不连接设备、不调用网络或模型，不读取旧库/根 `config.json`；只接受项目内相对 `--db`，已存在 v1 库只核验不改行。
8. 不读取根 `config.json`、环境文件或凭据；个人事实只经显式 `facts` CLI 与本机 profile 库存取，不进入测试夹具、策略包、公开材料或过程日志。不引入 Python 第三方依赖。
9. 过程日志是尽力而为的辅助产物：只追加固定字段，不写参数原值、serial、窗口原文、XML、身份键、正文、DB 绝对路径、异常或个人事实；入口忽略单次 `record` 的 `False`，但写失败或白名单拒绝会置 `degraded`，由 CLI 在 stderr 输出固定降级提示，不能声称日志完整。JSON 输出统一记录 `output` 成功/失败；ADB 命令与 store 调用记录 `started` 后再记 `ok/failed`；sense 解析也记录固定 `sense` 阶段。
10. M2 `sense` 必须先完成 manifest/profile/XML 校验与合并，结果 ok 后才初始化/打开本机库；复用 CLI 的同一 `ProcessLog`/correlation 记录 store open/upsert/unresolved，不预建或另造日志平台。
11. M3 `read` 只读合成 M3 manifest/profile/XML/像素，不连接设备、业务库、模型或网络，不 claim/start/finish、不关闭发现、不写正文或 verified 基线；真实采集适配器由测试以 mock subprocess 验证，M4c 已交付共享 OS 门禁且采集器每条子命令前复核，但仍不提供真实 `read --device` 入口。
12. M4a `policy` 只读公开 base/示例与固定 preview 案例，写本机 `prompts/local` 与 history；不连接设备、数据库、模型或网络，不读取根 `config.json`、私有事实、旧源码或黄金答案。四键错误外层是策略命令的明确扩展，其它命令保持三键；生产 `restore` 接真实 profile/共享占用检查，仅 stopped 允许，active/unknown/坏库/未决都拒绝且不换目录解锁。
13. M4a `domain/policy.py` 是 Envelope/Decision、策略包 schema、45 组合、摘要、渲染与披露复验的唯一纯实现；`ports/policy.py` 只声明文件与活动状态能力；`usecases/policy.py` 编排文件事务；`adapters/files/policy.py` 只做项目内 IO。M4b 桥/eval 与 M4c 真实检查不得另写一套契约或 JSON 出口。
14. M4c `domain/profile.py` 只做事实输入校验/状态筛选/待办，最终 allowed 仍由既有 `select_allowed_facts` 筛选；`ports/profile.py`/`ports/device_gate.py` 声明 profile 库与共享门禁；`adapters/sqlite/profile_schema.py` 复用 M1 v1 五表原文并追加最小 profile 元数据/事实/事实审计表，不改五表合同、不虚构迁移；`adapters/sqlite/device_gate.py` 固定 Windows `LOCALAPPDATA` 位置、唯一 primary、全会话先占后用、完整 owner/profile 身份复核、未知库不改写、异常隔离不自动解锁，并唯一记录 acquire/verify/release/quarantine；`adapters/sqlite/activity.py` 只读检查共享占用与 profile 库 M1 状态并记录 profile 打开；`adapters/sqlite/session.py` 提供真实 `ProfileSessionLease`，每次重核 profile 任务/设备租约与共享门禁；`usecases/facts.py` 做提案/确认/撤销/遗忘编排，值只在本机 CLI/库/请求内存；`usecases/acceptance.py` 只读复核凭证摘要；`usecases/run.py` 只做预检，不执行 M5 动作。真实 provider、真机与完整运行未接入。
15. M4b `usecases/decision.py` 只复用 M4a 唯一契约/渲染/校验与披露复验；`ports/decision.py` 声明 A/B 桥、当前包快照、可信租约与可注入时钟；`adapters/llm/` 只用参数数组、`shell=False`、`stderr=DEVNULL`、20 秒总预算与临时 stdin/stdout 文件，预算覆盖临时创建/输入写/Popen 边界，stdout 只读 `64KiB+1` 并在运行中监测增长，监测/等待 OSError 受控；固定 system/developer/user 三角色、无工具无历史，不支持角色分区即拒绝。只有传输层可证明进程未启动时可重试一次；重试前用唯一 render 校验器复验时钟/租约/包与事实有效期，提交后超时/schema/回显/证据失败不重发。
16. M4b `policy eval` 必须实际加载当前包并经同一桥路径；公开套件与旧 24 npm 回归独立，逐例映射只登记责任，不把旧参考输出回填新 Decision；模拟 transport 不生成验收凭证，真实外发需显式 `--confirm-external-send` 且该确认不授予设备/事实/业务发送权限。报告/凭证只写 `data/evals`；缺套件/零样本 not_measured 且 zero authorize，缺 public suite 与 authorize 拒绝的日志只记 started→failed；public/holdout 不读取无关 regression 准入，regression 仅纳入 registry 已确认退役的准入；留出访问 intent 在调用前锁内持久化、样本指纹占用历史、退役写实际 regression 准入且登记失败保留未确认准入但 registry 过滤不可运行；私有留出另需绑定当前 model/config 的本机无上传合同，合同只是 operator/host 声明。

## 四、测试与验证

在项目根执行：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m app doctor --fixture tests/fixtures/doctor/ready.json
python -m app store init --db data/runtime/m1-acceptance.sqlite3
python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3
python -m app read --fixture tests/fixtures/read/page.json
python -m app read --fixture tests/fixtures/read/conversation.json
python -m app policy init --dest prompts/local/demo
python -m app policy validate --pack prompts/local/demo
python -m app policy preview --pack prompts/local/demo --case policy-greeting
python -m app policy list
python -m app policy snapshot --pack prompts/local/demo
python -m app facts set --profile demo --key work_location --value "<合成值>" --confirm
python -m app facts show --profile demo --pack prompts/local/demo
python -m app run check --pack prompts/local/demo --mode read_only --confirm-device
```

`tests.test_doctor` 覆盖 M0 CLI、配置与夹具校验、设备解析、日志与 fixture 零子进程；`tests.test_process_log_concurrency` 用两个独立进程验证默认与显式日志路径的隔离和完整关联；`tests.test_store` 覆盖候选入库、竞争、TTL、三态收敛、恢复、clock_changed、严格输入与事务回滚；`tests.test_migrations` 覆盖空库建表/核验、幂等、初始化回滚、schema 篡改与 `store init` CLI；`tests.test_sensor` 覆盖 M2 解析/合并/聚合/停止原因，`tests.test_sensor_store` 覆盖 sense 落库、重放、终态与仓储错误；`tests.test_read` 覆盖 M3 页面/职位/未读/会话、字段阶梯、像素、稳定消息身份、跨屏合并、门禁与 CLI，`tests.test_screen` 覆盖 mock ADB 采集与共享门禁；`tests.test_policy_contract`/`test_policy_pack` 覆盖 M4a Envelope/Decision、摘要、事实交集、2/4/39 硬下限、唯一内容对象 64 KiB、渲染与披露/事实值复验，`tests.test_policy_store` 覆盖六命令文件事务、复制源重读、四键/配置错误外层、链接候选 list、load/validate/render/publish 阶段日志、坏 profile 库 unknown 拒绝与 LF/CRLF。`tests.test_profile_facts` 覆盖事实提案/确认/再显示/冲突/撤销/遗忘/无值审计/值不进日志/pack-profile 身份/旧库不导入；`tests.test_device_gate` 覆盖固定 OS 路径、跨进程/跨项目根互斥、完整 owner/profile 篡改、未知库不改写、崩溃/PID/TTL/nonce 篡改/库不可读/隔离与 profile 活动检查；`tests.test_run_guard` 覆盖 read_only 确认/draft 验收/A 档拒绝/跨库隔离/日志配对；`tests.test_profile_session_lease` 覆盖合法 profile 库经唯一 SqliteStore 领取/续租/失效与 decide 桥前后拒绝；`tests.test_m4c_closed_loop` 覆盖真实活动 restore、目录不发布、三值一致、事实/任务审计不重置、旧凭证失效、唯一仓储快照重核与实际 CLI 串行闭环。M4b `tests.test_bridge` 覆盖 A/B 参数数组真实子进程、固定三角色、启动失败一次重试、重试前时钟/包/事实有效期门禁、提交后超时、临时创建/写超预算零 Popen、2MiB stdout 上限、慢/不读 stdin/大 stderr/短命继承 stdout、输出监测或 wait OSError、认证/额度/断连/角色/协议固定诊断与事实复验；`tests.test_policy_eval` 覆盖实包加载、模拟/真实适配器路径报告绑定与 correlation、公开/回归/留出分桶、24 行映射、失败/模拟/零样本无凭证、编辑包改变请求/摘要、留出 intent/中断/写失败/并发/样本身份占用/退役实际 regression/登记失败清理准入/无上传合同/文件边界/无关坏 regression 准入不影响 public+holdout/job_card 正控制/缺 public 与 authorize 拒绝日志唯一终态。真实 ADB 模式只通过 mock 的 subprocess 输出验证，不代表已在真机运行；M1/M2 测试只用合成库与注入检查器，M2/M3 只用合成 XML/RGBA，M4a/M4b/M4c 只用临时项目根、临时 OS 根、合成 preview/套件/事实/桥桩与跨进程模拟；真实 profile 租约只接合成 profile 库，真实 provider、私有留出、真实 OS 门禁目录与真机未运行。

## 五、维护要求

1. 新增、删除、改名或移动本目录文件/子目录时，同步更新本文件和根 `AGENTS.md`。
2. 新增承载真实代码的子目录时，同目录创建 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`；不创建空目录。
3. 每个源文件顶部说明用途，每个函数/方法写 docstring；修改模块时同步对应子目录指南。
4. 新增里程碑能力时先确认计划路径、`app/` 分层和测试入口，再按对应 M 的验收契约实现，不提前创建空模块。
5. 涉及真实设备、外部模型或网络时先取得本轮确认；当前不提供这些调用的默认入口。

## 六、已知限制

- M1 只实现设计说明 §6 的五表 v1、任务租约与 `store init`；M2 只实现合成 XML 解析/未读快照入库，不 claim/start/finish、不推进已读基线；M3 只实现离线页面/职位/未读/会话读取、字段阶梯、像素与跨屏身份，不调用仓储、不导航、不执行动作；M4a 只实现决策契约、策略包/摘要/渲染、六命令文件事务与真实活动恢复接线；M4b 只实现 A/B 本机子进程桥、单次决策编排、分层 eval/留出事务与无上传合同门槛；M4c 只实现 profile 事实库/本机 facts、固定 OS 共享门禁/restore 检查/验收复核/run check 预检，不执行 M5 动作，不接真实 provider、真机或完整运行。
- 真实 `adb devices` / `dumpsys window` / `uiautomator dump` / `screencap` 未在本机执行，真实机型、系统版本、序列号格式、窗口/截图输出差异未验证。
- `doctor` 只做一次独立探针，不提供业务循环、导航、截图或动作；`store init` 不做真实业务调度；`sense` 只读合成夹具；`read` 只读合成输入且不提供真实 `--device` 入口；`policy` 只在离线合成资源上运行；`policy eval` 只用本机桥桩/模拟 transport，未调用真实模型；`facts`/`policy acceptance`/`run check` 只用临时项目/OS 根与合成事实，`run check` 不执行设备或动作。
- M1 的 v1 只允许 app=boss；第二应用启用时需后续版本迁移，不预建迁移框架。
- M2 合成 profile 与 M3 合成 profile 不是已验证真实版本；像素阈值、稳定消息标识、新招呼真实语义与滚动分页仍待真机标定。M4a base/示例虽为公开交付，但 preview 通过不证明模型语义安全、A 档判据或发送授权。M4b 桥的角色/工具/历史声明只是协议声明，不证明外部 provider 实际行为；无上传合同只是 operator/host 显式声明，不证明子进程真实无网络；公开 eval 通过不证明语义安全或真实模型能力。M4c 共享门禁只在临时 OS 根模拟，未验证真实 `LOCALAPPDATA`、真实崩溃/并发、真实 ADB 或 M5 对账开锁；真实 profile 租约只在合成 profile 库上验证，未接 M5 动作/调度。`run check`/验收复核通过不构成发送授权或完整 MVP 验收。
- 过程日志没有轮转、保留期、远程汇聚或审计保证；写入失败时只能通过固定 stderr 降级标记和缺失行发现，磁盘失败时日志不完整。
