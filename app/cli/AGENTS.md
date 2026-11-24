# app/cli 目录指南

## 一、目录用途

提供 M0/M1/M2/M3/M4a/M4b/M4c/M5 的命令行组合根：严格解析全局 `--config`、`doctor`、`store init`、`sense`、`read`、`policy`、`facts`、`run check` 与 `action` 子命令参数，校验项目内相对路径，加载最小配置与合成夹具，注入只读探针、SQLite 初始化、BOSS 合成解析、M3 读取、M4a 策略文件工作流、M4b A/B 子进程桥与评测文件存储、M4c profile 事实库/共享设备门禁/活动检查/验收复核，以及 M5 合成动作执行器/文本资格/人工卡/受控对账，并只向 stdout 输出一个 UTF-8 JSON 对象。

## 二、当前文件

- `__init__.py`：声明命令行包。
- `main.py`：`main(argv, process_log=..., os_root=..., stop_checker=...)` 入口（`os_root`/`stop_checker` 仅供测试注入临时应用数据根与合成停止判据）、严格 argparse、配置校验/夹具读取、共享 JSON 重复键边界、fixture 与 ADB 组合、`store init --db` 接线、`sense --fixture/--db` 接线、`read --fixture` 接线、`policy init/validate/preview/list/snapshot/restore/eval/holdout/acceptance` 接线、`facts set/show/revoke/forget` 接线、`run check` 接线、`action perform/eligibility/card/reconcile` 接线、共享设备门禁取得/复核/释放/受控恢复、唯一安全 JSON 输出、退出码与过程日志阶段记录。M4b `eval` 只接受 `external-agent`/`local-process` 显式 provider、参数数组桥程序与 `--confirm-external-send`；`holdout register/retire` 只维护本机冻结登记；`action perform` 只接受 `draft_only`、当前验收、本轮动作授权与合成脚本，不提供真实设备动作入口。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、行为契约

1. 全局 `--config` 必须放在子命令前；`doctor` 的 `--fixture` 与 `--device` 互斥且必须选其一；`--device` 还必须带非空 `--serial` 与 `--confirm-device`。`--confirm-device` 不能与 fixture 同用。
2. `--serial` 只能与 `--device` 同用且此时必填；空白 serial 视为缺失。
3. 未知选项、选项缩写、重复选项、缺参数、额外位置参数统一输出 `invalid_input` 并退出 `2`。
4. 配置省略或内容为 `{}` 时保持 `{"schema_version":1,"mode":"read_only"}` 契约；`_validate_config` 只做校验，不返回未使用的对象，只接受这两个键与固定值。
5. 配置、夹具与 `--db` 路径只接受项目内相对路径；拒绝绝对路径、盘符、`~` 和解析链接/`..` 后越界。`store init --db` 省略时为 `data/runtime/runtime.sqlite3`。
6. 参数/配置/夹具/仓储错误只输出三键安全对象，不回显输入、异常、绝对路径或命令参数。共享 JSON 边界拒绝重复对象键（含嵌套）；夹具还拒绝不可 UTF-8 编码的 alias/selected_alias。
7. fixture 模式不得实例化或调用 ADB 适配器，不得启动子进程；CLI 导入适配器模块本身必须零副作用。`store init` 只调用 SQLite 初始化/核验，不启动子进程、不调用网络。
8. JSON 输出统一经过 `_finalize_output`：安全 `emit`、记录 `output` 成功/失败、再记录 `exit`；输出失败时安全退出 `2`、不打印 traceback，日志明确为 `output=failed`，不能假称 JSON 已输出。成功或业务停止正常时 stderr 为空；日志写入失败时 stdout/退出码不变，stderr 仅写固定 `m0_process_log_degraded`。`--help` 保持 argparse 直接输出并退出 `0`，只记录其 output/exit 路径，不伪造业务 JSON。其余失败退出 `2`，不使用退出 `1`。
9. `store init` 成功只输出 `{"schema_version":1,"status":"ok","reason_code":null}`；失败原因取 `invalid_input/store_busy/store_io_error/db_version_unsupported/migration_failed` 等仓储词表，不新增业务成功字段。
10. `sense` 必须给 `--fixture`，`--db` 可省略为 `data/runtime/runtime.sqlite3`；不接受 `--device`/`--serial`。只有 manifest/profile/XML 全部通过且合并 `status=ok` 后才初始化/打开 SQLite；先 upsert 再查 `unresolved_conversations` 并聚合。成功/业务停止输出 `{schema_version:1,result:<ReadResult>,stop_reason:<固定原因或 null>}`；`stop_reason=null` 退出 `0`，其它退出 `2`。解析器已构造 ReadResult 的非 ok 结果保留其完整输出且不写库；仓储错误只输出三键安全对象，不伪造成功 ReadResult。
11. 过程日志阶段覆盖 `start/arguments/config/mode/fixture/probe/adb_devices/adb_window/decision/store/sense/read/adb_hierarchy/adb_screen/adb_evidence/output/exit`；store 调用先记 `started` 再记 `ok/failed`，`operation=init/parse/open_store/upsert_snapshot/claim/start/renew/finish/recover/unresolved_conversations`，`read` 阶段取 `load/page/job_list/job_detail/unread/conversation/merge/pixel/gate`；结果含 `started/ok/undetermined/failed`。已知 `StoreError` 的失败事件使用固定仓储原因与 `error_category=store`，非预期异常只写失败事件 `reason_code=null`/`error_category=internal`；对应业务 stdout 仍为 `store_io_error` 和退出 `2`，最终 exit 的公共错误分类与失败事件分类分别记录、互不替代。sense/read 解析复用入口同一日志实例/correlation；`read` 的 load、解析、像素、merge、gate 均成对记录，未标定/未决记 `undetermined`；`ok` 但门禁失败时 `gate/decision=failed` 且带固定 gate 原因。不写 XML、身份、正文、像素、原始稳定 ID 或夹具/数据库路径。每次运行由 `ProcessLog` 生成独立 UUID 文件，显式路径也只作命名基础；日志降级时由唯一 `emit_degraded_warning` 写一次固定 stderr 标记，CLI 与仓储 API 不双报；stdout 不包含 `correlation_id`。
12. `read` 必须给 `--fixture`，不接受 `--device`/`--serial`/`--db`；输出 `{schema_version:1,result:<ReadResult>,actionable:<bool>,gate_reason:<null|fixed>}`，`status=ok` 且 `actionable=true` 退出 `0`，其它退出 `2`。`read` 不调用仓储、不写库；真实设备入口在共享门禁交付前不存在。
13. `policy` 子命令为 `init --dest`、`validate --pack`、`preview --pack --case`、`list`、`snapshot --pack`、`restore --snapshot --dest`、`eval --pack --suite --provider --bridge-command [--arg] --model-id [--confirm-external-send]`、`holdout register/retire`、`acceptance check --pack --credential [--provider --model-id --model-config-hash]`；全部复用同一组合根、`_finalize_output`、唯一 `ProcessLog`/correlation，不另造 JSON 出口或日志器。`facts` 为 `set/show/revoke/forget`，`run` 为 `check`；facts 成功/提案为四键 `status=ok|confirmation_required`，错误三键固定原因；`run check` 成功三键并输出预检结果。成功四键 `{schema_version:1,status:"ok",reason_code:null,result:...}`；失败四键 `{schema_version:1,status:"failed",reason_code:<固定>,errors:[{file,field,line,code}]}`，`errors` 可为空。`policy` 参数形状错误、全局配置受控错误与配置读取意外异常都使用四键（配置错误保留 `invalid_config`），其它命令保持三键；只依据 argv 第一个位置参数识别命令，不因任意值含 `policy` 改变外层。
14. `policy` 只接受项目内相对路径：`init`/`restore` 目标为 `prompts/local/<profile>` 直接子目录，`snapshot` 目标为 `prompts/local/history/<snapshot-id>`，preview 案例固定 `tests/fixtures/policy-preview/<case>.json`，eval 套件固定 `evals/policy-suite/public.json` 与忽略的 `evals/private/`，验收凭证只读 `data/evals/eval-<32hex>/acceptance.json`。不读取根 `config.json`、设备、网络、模型或黄金答案；生产 `restore` 注入真实 `ProfilePolicyActivity`，active/unknown/坏库/未决均 `policy_changed`，不新增公开测试绕过参数。`facts` 只写 `data/runtime/profiles/<profile_id>/runtime.sqlite3`，带 `--pack` 时 pack.profile_id 必须等于 `--profile`，否则 `profile_mismatch`；路径存在但非普通文件时 `profile_io_error`，不伪造空结果。`run check` 用同一 profile 固定路径与临时/固定 OS 根门禁，不执行动作。
15. `policy` 过程日志使用 `stage=policy`，命令操作 `init/validate/preview/list/snapshot/restore/eval/holdout/acceptance`，并由 usecase 在实际边界追加 `load/validate/render/publish` 与 `eval/suite|authorize|case|report|acceptance|holdout`、`decision/verify|bridge|validate` 阶段；`error_category=policy/eval/decision/bridge` 与固定原因码；`facts` 用 `stage=facts` 与 set/show/revoke/forget，并和 run/restore 一样经 `app/profile_open.py` 记录 `profile/open_profile` started/终态；`device_gate` 只由门禁适配器记录 acquire/verify/release/quarantine/recover 的 started/终态，`run` 用 check；CLI 不重复记录门禁适配器同一操作；不写事实值、profile ID、门禁路径、正文、argv、凭据或异常原值；缺 public suite 的 suite 阶段与 authorize 拒绝均只记 started→failed，不先 ok；不写文件正文、事实值、回复、argv、凭据、绝对路径或原异常；日志失败不改变业务输出/退出码。报告写入同一 `correlation_id`，逐例只带安全 `case_index`。
16. `action perform` 只接受 `--mode draft_only` 且必须 `--confirm-device --confirm-action` 和至少一个 `--authorize`；执行前及每条命令/提交前重读当前 base/pack 与当前验收，并由用例复核实际 store/profile、running 租约与共享身份。seed 只读合成 M2 夹具，动作脚本只作为 `ScriptedActionExecutor` 观察输入；动作计划必须带 `--contact/--company/--position` 结构化身份，三元组重算的会话键必须与持久任务一致，缩减/冲突在用例 validate 阶段拒绝。成功四键输出动作状态/完整七字段证据/人工卡/资格；失败三键固定原因并隔离占用。`action eligibility` 只读计算资格并固定 `policy_tier=C`/`production_auto_send=false`，同动作历史返回 blocked/conflict；`action card` 只读人工卡；`action reconcile` 只接受原 profile/原库共享占用与本轮确认，confirm 需现场/IME/原 host/原命令族停止和当前证据，abandon 只关任务且可之后受控 confirm；可选 `--device --serial` 以现有 `AdbRecoveryObserver` 一次读取实际层级/IME，坏 XML/错 IME 拒绝，fixture 只提供本轮证据。`main` 的 `stop_checker/command_stop_checker` 仅为测试注入；生产命令检查器优先使用按动作分段记录的命令族账本，缺失/损坏/未闭合保持 unknown。CLI 不提供真实 ADB 动作入口，也不把合成授权当真实发送授权。
17. 过程日志动作阶段使用 `stage=action`、`operation=perform/eligibility/card/reconcile/begin_action/complete_action/reconcile_action/text_admission/task_conversation/require_running/save_ime/action_validate/action_admit/action_execute/action_derive/action_commit/action_reconcile_validate/action_reconcile_commit` 与固定原因码；ADB 步骤用 `adb_action_dump/cat/input/tap/ime_get/ime_set` 有限标签；不写正文、附件名、坐标、授权原值、事实值、路径或异常原文。
16. `policy eval --help` 必须列出受支持 provider 格式、协议版本、认证变量/端点边界（本程序不读取值）、本机要求、固定诊断、留出无上传合同路径与 `data/evals` 产物边界；`--confirm-external-send` 只确认本轮模型外发/计费，不授予设备、事实写入或业务发送权限，也不替代留出无上传许可。拒绝 `echo`、`reference-provider`/smoke 冒充与未知 provider。

## 四、测试

```powershell
python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen tests.test_policy_store tests.test_profile_facts tests.test_device_gate tests.test_run_guard tests.test_m4c_closed_loop tests.test_bridge tests.test_policy_eval tests.test_action_executor tests.test_action_cli
```

`tests/test_doctor.py` 覆盖 M0 配置/夹具负例、重复 JSON 键、孤立代理字符、模式冲突、重复/未知选项、帮助 output/exit、业务失败、设备失败七键映射、窗口字段边界/解析、stdout 写失败、`output=failed`、白名单拒绝降级提示、日志 started→结束顺序与 fixture 零子进程断言。`tests/test_migrations.py` 覆盖 `store init` 的固定 JSON、幂等、默认路径、路径越界、损坏/篡改/未知版本库与失败日志。`tests/test_sensor_store.py` 覆盖 `sense` 正例/重放、终态、停止原因、仓储错误、日志关联与零设备/网络/进程断言。`tests/test_read.py` 覆盖 `read` 五类 fixture、门禁、参数边界、零设备/库/子进程与日志阶段；`tests/test_screen.py` 覆盖 mock ADB 采集与共享门禁；`tests/test_policy_store.py` 覆盖 `policy` 六命令四键外层、参数错误区分、文件事务、坏 profile 库 unknown 拒绝、日志与零设备/网络/子进程；`tests/test_profile_facts.py` 覆盖 facts 提案/确认/撤销/遗忘/日志/包身份；`tests/test_run_guard.py` 覆盖 run check 确认/验收/A 档/日志配对；`tests/test_m4c_closed_loop.py` 覆盖 acceptance check、真实活动 restore、目录不发布、实际 CLI eval 闭环；`tests/test_profile_session_lease.py` 覆盖真实租约适配；`tests/test_bridge.py`/`tests/test_policy_eval.py` 覆盖 M4b A/B 子进程、授权拒绝、报告/凭证、分桶、映射与留出 CLI；`tests/test_action_cli.py` 覆盖 `action` 五动作正反例、资格、人工卡、对账、abandon 隔离与零真实子进程。

## 五、维护要求

1. 修改参数、配置、路径或错误出口时，同步更新本文件、根 `AGENTS.md` 和 `docs/operator.md`。
2. 新增子命令时先对照实施计划的里程碑契约；不得把后续里程碑子命令提前接入。
3. 新增文件/子目录时同步更新本目录索引；不另建重复全局规则的文档。
4. 保持组合根是唯一可导入适配器的 CLI 模块，不让 usecase 或 port 反向依赖具体通道。

## 六、已知限制

- 当前只有 `doctor`、`store init`、`sense`、`read`、M4a `policy`、M4b `policy eval`/`policy holdout`、M4c `facts`/`policy acceptance`/`run check`、M5 `action perform/eligibility/card/reconcile`；M6 调度与完整 run 未实现。
- 真实设备模式只做离线 mock 验证，尚未在真机执行；M1/M2 只用合成库与合成 XML，M3 只用合成 XML/RGBA 与 mock ADB，M4a 只用临时项目根与合成 preview 案例，M4b 只用合成本机桥桩与模拟 transport；M5 只用合成动作脚本、mock ADB 与临时 profile 库；真实 provider、私有留出、真实租约/活动与真机动作未接入。
- 输出键顺序固定，但调用方应按键读取而非依赖顺序。
- 过程日志为 best-effort；调用端忽略单次 `record` 的 `False`，只通过降级标记暴露不完整，不能当作审计保证。
- M4a CLI 的 `restore` 已接真实活动检查，仅 stopped 允许；测试用坏 profile 库/共享占用覆盖 active/unknown 拒绝，正路径用临时项目根与真实 stopped 检查，不代表真机或完整恢复对账已验收。`run check` 只是预检，不执行设备、模型或动作。
