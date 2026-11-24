# framework 目录指南

## 一、目录用途

提供标准库实现的资产校验、严格比较、虚拟时钟/事件重放与验收分流。它只判断测试资产和真实产品输出，不实现未读聚合、调度、动作执行或任何业务替代逻辑。

## 二、文件清单

- `__init__.py`：包标识。
- `model.py`：加载 catalogue/oracles，校验 73 场景、变体引用、设备门槛、夹具结构与路径边界；错误不带绝对路径。
- `compare.py`：JSON 类型严格比较（bool≠整数）、路径/键集合/长度断言、数组每个元素的固定键集合与必要字段类型（`element_keys`/`element_types`，支持可空联合类型）、普通业务 stdout 必须是单个 JSON 对象；不只检查数组首项。
- `replay.py`：虚拟时钟与固定事件序列重放，校验起点/单调/非 bool 非负，保留 arrival/first_observed/handling_started 为独立字段。
- `m3_probe.py`：M3 受限探针，调用真实 `targeted_verification_outcome`、`load_and_read_fixture`/`new_message_keys` 与 `resolve_field`；用于 CLI 无法表达的显式 now、跨快照新增键、阶梯/上下文冲突与证据输出，不复制业务判定；路径先查原始 leaf/祖先链接再 resolve。
- `m4c_probe.py`：M4c 受限探针，调用真实 `SqliteProfileStore`/facts、`SharedDeviceGate`、`ProfileSessionLease`、`ProfilePolicyActivity`、`run.check_preflight`、真实 snapshot/restore 与 acceptance 复核；使用隔离 profile 库/os_root/barrier/固定时钟，支持两进程竞争、崩溃/TTL/PID/换 profile/字段篡改、失败 quarantine、事实过期/冲突/白名单/跨 profile、真实租约桥前后拒绝；路径先拒绝原始 leaf/祖先 symlink/junction 再 resolve，只输出状态、计数和无值摘要。
- `m4_probe.py`：M4 受限探针，调用真实 `FilePolicyRepository`/`load_active_pack`/`validate_pack`、`parse_envelope`/`render_request`/`parse_decision`/`validate_decision`、`decide` 与 `eval_pack`/留出事务；只替换 clock/snapshot/lease/bridge transport 边界，支持决策变异、摘要关系、事实/档位、真实 LocalProcessBridge 载荷捕获、合成 restore activity 与公开/留出评测摘要；不返回正文、事实值、包全文或绝对路径。
- `m2_probe.py`：M2 受限探针，调用真实 `load_and_parse_snapshot`/`persist_and_aggregate`；仅用于 now 注入、合成初态 seed、只读 baseline/inspect，SQL 不替代生产转换；inspect 返回发现/租约/基线/消息/设备只读事实，baseline 记录完整五表+事件签名、“基线/消息/设备”稳定签名与被保护任务签名（仅允许 source_names/updated_at 变化）；request/db/fixture 先查原始 leaf/祖先链接，再 resolve/containment。
- `m1_probe.py`：M1 纯合成 JSON 脚本探针；只调用真实 `initialize_database`/`SqliteStore`，支持假时钟、固定 host/owner、停止检查器、显式 ProcessLog、受限 prepare/inspect 与 barrier/hold_lock；request/db/log/barrier 路径在读写开库前校验为隔离根内相对路径，拒绝绝对/..、自身或祖先链接；不接受任意 SQL/命令/路径。
- `m0_protocol_probe.py`：在隔离根仅替换 `subprocess.run` 设备边界，调用已交付 `AdbDeviceProbe` 验证超时/非零/不重试；不启动真实 ADB。复制该入口前复用父路径 symlink/junction 与 target 边界校验，不只检查文件存在。
- `runner.py`：`assets`/`acceptance` 分流，含 `IMPLEMENTED_MILESTONES` 门禁（当前 M0–M4）；acceptance 先校验资产，再在隔离根复制真实 app 与命令/manifest/read fixture 选定的合成输入（排除 expected/oracle），按变体声明复制公开 base/preview/套件、`local_packs` 到 `prompts/local`、`fixture_copies` 与受控合成 `junctions`，使用最小环境白名单和 tripwire 拒绝网络/子进程，缺 app 记 BLOCKED；并发组按声明校验子进程输出与 `children_relation` 的唯一 winner/loser 完整身份关联。过程日志按单次 CLI 调用快照新文件并与该步骤绑定，检查单一 correlation、固定字段白名单/类型、完整有序 stage+operation+result+reason_code 序列、started 成对次数、禁用操作与敏感标记；日志目录自身/祖先链接在读取前拒绝。
- `AGENTS.md`：本文件。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、入口与行为

```powershell
python -m tests.scenarios.framework.runner --mode assets
python -m tests.scenarios.framework.runner --mode acceptance --target .
```

- assets 只看测试资产，退出 0 不是产品 PASS。
- acceptance 缺 `app/__main__.py`、里程碑未实现、入口未冻结、DEFERRED 或 MODULE_EVIDENCE 退 3；任一执行差异退 1；只有全部 F 变体执行通过才退 0，且报告仍标记设备门槛未验收。当前门禁为 M0–M4；新增里程碑须先更新常量与指南。并发组只由父 runner 启动两个隔离子进程，全部 ready 才 go，spawn 失败/提前退出/超时均 FAIL，并在 try/finally 有界终止/回收全部已创建子进程；timeout 必须为 1–120 秒非 bool 整数，非法时零启动；子进程 tripwire 仍禁设备/网络/孙进程。
- 报告只写 `data/evals/scenario-runs/<run>/report.json`；隔离根同在其下，含 app 副本、夹具副本、tripwire 日志与临时库。

## 四、维护要求

- 不在此实现生产业务函数，也不 mock 被测业务函数；只替换设备/时钟/provider 等边界。
- 修改比较器或隔离逻辑必须补正反例：bool 充整数、非法 JSON、时钟回拨、路径越界、同名前缀目录、symlink/junction、凭据继承、网络/子进程 tripwire。
- 不输出环境变量值、原始异常链或本机绝对路径；新增错误只保留相对标识与异常类别。
