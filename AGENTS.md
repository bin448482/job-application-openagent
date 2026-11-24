# job-application-openagent 项目指南

## 一、项目基本信息

正式名称为 `job-application-openagent`，依据 `README.md` 与 `pyproject.toml` 的 `project.name`。根目录为本指南所在目录，运行路径相对于项目根目录解析。

本项目面向开源维护的 Android 求职辅助工作流，目前处于分阶段实现阶段。M0 只读探针、本机白名单过程日志、M1 状态模型/租约、M2 合成未读四源传感器、M3 离线读取阶梯/页面/职位/会话读取、M4a 决策契约/策略包/离线编辑回滚与 M4b A/B 本机决策桥/分层 policy eval/留出冻结登记已实现；旧业务源码、Skills 与业务测试已清理，不恢复旧实现或个人授权。当前已有独立的 promptfoo 合成黄金集，检查记忆与授权决策；M4c 运行绑定、真实活动检查、事实确认入口、动作、调度、运行时决策记忆、设备执行器和完整业务范围仍待实现。真实 provider、私有回归/留出与真机均未运行。

本仓库自身：业务项目要求 Python >=3.13，Python 依赖保持为空；评测开发工具另需 Node.js >=22.22.0、npm 和固定版本 promptfoo 0.126.0。评测使用 CommonJS、Node 内置测试和 JSON 配置，npm 依赖通过 package-lock.json 锁定。引入 promptfoo 是当前明确的评测需求，不改变 Python 标准库优先约束。

文档所描述的消费方：评测可接受 promptfoo provider ID 连接模型或自定义端点，认证由使用者按 provider 类型配置，不提供默认真实模型。README 约定未来设备功能需要 Android SDK Platform-Tools 的 adb、USB 调试及使用者确认；当前没有业务 Skill 或设备工作流，不推定宿主、机型、包名或发送授权。

主要入口与模块关系：README 提供使用概览；`python -m app` 进入 app/cli/main.py，当前分派只读 `doctor`、`store init`、合成 `sense`、合成 `read` 与离线 `policy`（含 `eval`/`holdout`）。fixture 由 CLI 读取，真实探针经 app/ports/device.py 注入 app/adapters/adb/device.py，业务判定在 app/usecases/doctor.py；`store init` 经 app/adapters/sqlite/schema.py 建库/核验，仓储实现在 app/adapters/sqlite/repo.py，领域模型与键/摘要算法在 app/domain/models.py，端口在 app/ports/store.py。M2 的 `sense --fixture` 由 app/adapters/boss/list.py 解析合成 manifest/profile/XML、分页合并，经 app/usecases/sense.py 调用同一 `StorePort` 的 upsert/unresolved 和 app/domain/scheduler.py 的纯聚合/停因；只有合并 ok 后才打开本机库。M3 的 `read --fixture` 由 app/adapters/boss/read.py 解析页面/职位/未读/会话并调用 app/adapters/boss/pixel.py 与 app/domain/ladder.py，决策门禁/定向核对/JSON 在 app/usecases/read.py；采集端口在 app/ports/screen.py，真实 ADB 采集在 app/adapters/adb/screen.py。M4a 的 `policy` 由 app/cli/main.py 接线 FilePolicyRepository（app/adapters/files/policy.py）与 app/usecases/policy.py，DecisionEnvelope/Decision、包 schema/45 组合/摘要/渲染在 app/domain/policy.py，端口在 app/ports/policy.py；公开 base/示例在 prompts/base、prompts/examples/default，preview 合成输入固定在 tests/fixtures/policy-preview。M4b `policy eval` 由 app/cli/main.py 接线 ExternalAgentBridge/LocalProcessBridge（app/adapters/llm/）、FileEvalStore（app/adapters/files/evals.py）、app/usecases/decision.py 与 app/usecases/policy_eval.py；桥/快照/租约端口在 app/ports/decision.py，套件/报告端口在 app/ports/eval.py，公开套件与旧 24 映射在 evals/policy-suite。白名单过程日志由 app/process_log.py 每次运行写到被忽略的 data/logs/m0-process-<运行 UUID>.jsonl（阶段含 `store/sense/read/policy/eval/decision/adb_hierarchy/adb_screen`），M1/M2 业务库写到被忽略的 data/runtime/，M4b 报告/凭证/留出登记写到被忽略的 data/evals/。package.json 的 npm scripts 调用 evals 的断言测试或评测入口，promptfooconfig.json 加载提示词、24 个案例和 JavaScript 断言。smoke 仅回放参考输出，model 才使用显式 provider；case metadata 不渲染进模型提示词。评测产物仅保存到 data/evals。pyproject.toml 为业务元数据，不是已实现的 CLI 或发行包。每个目录的 CLAUDE.md 仅引用同目录 AGENTS.md，全局规则只在根指南保存。

新环境自动化部署方案已形成设计文档，建议首批 Windows 11 x64 / Windows PowerShell 5.1，分别验收 Node 合成评测、ADB 工具准备和经本轮确认后的手机环境检测；Python venv、模型认证、Android 与开发编排工具不作为当前评测必需。Android 检测不等待完整业务，但须先实现检查器与共享设备门禁；部署入口、引导发行物与检查器尚未实现，干净机器及手机验收均待执行，本轮仅文档、未运行 ADB；既有本机测试记录不能替代该验收。

项目特有约束与已知限制：

- 默认使用标准库，仅在真实需要时添加依赖；评测依赖按 npm 开发依赖管理。M1/M2 使用 Python 标准库 `sqlite3`、`xml.etree.ElementTree`，没有第三方 Python 依赖。源代码顶部说明用途，函数和方法写 docstring/JSDoc；修改模块时同步目录指南。
- 源码、环境与数据库只依赖当前独立项目，不依赖原项目虚拟环境、数据库或仓库外业务规则母本；当前不进行旧个人策略通用化，不恢复已清理业务代码与 Skills。
- 不扩展真实自动发送授权，不继承旧工作地点、简历事实或联系方式授权。黄金集中的城市、薪资、记忆、授权与 HR 消息均为合成输入；can_send=true 只是拟执行测试决策，不触发真实发送、记忆写入或手机动作。
- 数据库、日志、契约、截图、凭据和个人事实只在本机保存。不读取或记录密钥、令牌、密码等敏感值；不读取根 config.json 或私人事实来填充测试集。新增故障案例须先脱敏并确定独立判据，不直接把模型输出当正确答案。
- 操作手机前说明具体动作、预计占用时长和是否独占，并取得本轮确认；未确认不得连接设备或运行 ADB。npm 自检和黄金集评测均不操作设备。
- config.example.json 当前只含 `policy_pack`，指向被忽略的本机策略包；其余业务 schema、字段和默认值仍待设计。M0 最小配置规则已在 CLI 实现（只读模式两项键），不写入本示例文件。config.json 是被忽略的本机文件，不作为示例或授权来源，不沿用旧批次大小、每日额度或 HR 间隔。
- M2 `sense` 只接受合成 manifest/profile/XML，profile 是离线开发契约，不代表真实 BOSS 版本已标定；不得读取 expected.json、不得连接设备/模型/网络、不得 claim/start/finish、不得打开会话或推进已读基线。manifest 引用必须留在夹具目录内，拒绝绝对路径、`..`、链接与越界；未知字段不当零，过期/未来/未标定/非法 XML 不落库。
- M3 `read` 只接受合成 M3 manifest/profile/XML 与显式合成像素标定，不连接设备、业务库、模型或网络，不 claim/start/finish、不关闭发现、不写正文或 verified 基线。页面/职位/未读/会话值必须严格校验；会话正文/职位正文只在内存与合成 CLI 数据区，不写日志/事件/库。稳定消息身份只能来自 profile 验证过的应用 `content-desc`，内容指纹/同秒/坐标/屏幕序号/布局 resource-id 不能生成可发送入站键；未标定真实版本保持 unknown。真实采集适配器只在测试 mock subprocess；真实 read 在共享 OS 设备门禁交付前任何子进程启动前拒绝，profile 内 primary 任务锁不能替代。真机像素/字体/分辨率/稳定标识与只读一轮未测。
- M4a 只交付 Envelope/Decision 严格契约、TOML+三 md 策略包、45 组合与硬下限、四类无自引用摘要、system/developer/user 纯渲染、披露复验和 `policy init/validate/preview/list/snapshot/restore` 文件流程；不接事实确认、设备或模型。策略命令成功/失败使用四键 `errors` 外层，为既有三键的明确扩展；`base_hash` 使用 `prompts/base/<文件名>` 项目相对 POSIX 路径，preview 固定读取 `tests/fixtures/policy-preview/`（公开运行资源，部署须携带）。生产 `restore` 在缺 M4c 真实活动/未决检查时固定 `policy_changed` 拒绝，不把 unknown 当 stopped、不用占位 True；仅测试可向同一用例注入明确合成停止证据，完整 CLI 安全接线留 M4c。preview/validate/snapshot 通过不证明模型语义安全或发送授权，v1 无独立 A 档前置判据时固定 C。
- M4b 只交付 A/B 本机子进程决策桥、单次决策桥前/桥后复验、`policy eval` 实包分层评测与 `policy holdout register/retire`；不接真实 provider、M4c 真实活动/事实确认、M5 授权/动作或设备。桥只用 argv 数组、`shell=False`、20 秒超时与有界 stdout，固定 system/developer/user 三消息与独立回显，不接收输入指定 role/tool/message；角色/工具/历史声明只是协议声明，不证明第三方实际能力。只有传输层可证明进程未启动时重试一次，提交后超时/schema/回显/证据失败丢弃且不重发。`policy eval` 必须实际加载当前包并经同一桥路径，公开套件与旧 24 npm 回归独立；映射只登记责任，旧 expected/reference 不得回填新 Decision。报告/凭证只写 `data/evals`，失败/零样本/非公开集/simulated 不生成有效验收凭证；`--confirm-external-send` 只确认本轮模型外发与计费，不授予设备/事实/业务发送权限。留出只允许本机 provider，冻结登记、访问轮次、摘要变化整批退役与零样本未测由实现/测试覆盖；真实 provider 与私有留出明确未测。
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

- `README.md`：当前范围、M0 只读探针、M1 状态库、M2 合成传感器、M3 离线读取、M4a 策略包离线命令与 M4b 桥/eval、业务和评测环境、Python/npm 命令、数据边界与未实现部分；可独立阅读的使用概览，不是全局规则母本。
- `LICENSE`：MIT 许可证，2026 年项目贡献者署名。
- `app/`：M0–M4b 分层程序包。`__main__.py` 提供 `python -m app`；`cli/` 负责严格参数、配置、路径与 JSON 输出，接线 `doctor`、`store init`、`sense`、`read` 与 `policy`（含 `eval`/`holdout`）；`domain/` 提供纯领域模型、H/时间/键算法、严格校验、共享严格 JSON、M2 聚合/停因、M3 读取值类型/字段阶梯、M4a 决策/策略包/摘要/渲染与 M4b 套件/断言/留出校验，不导入适配器；`ports/` 声明设备探针、`ScreenPort`/共享设备门禁、`StorePort`、M4a 策略文件/恢复活动状态、M4b 决策桥/当前包快照/可信租约与评测文件端口；`usecases/` 做输入校验、唯一 doctor 结果组装、sense 持久化/聚合编排、read 门禁/定向核对/JSON、policy 文件事务、单次决策编排与 policy eval/留出编排；`adapters/adb/` 实现仅显式触发、只接受 tab 分隔确定清单记录的真实 ADB 探针及 M3 层级/截图采集；`adapters/boss/` 实现 M2 合成 profile/manifest、XML 四源解析/分页合并与 M3 页面/职位/未读/会话、像素和跨屏合并；`adapters/files/` 实现 M4a 项目内策略读取/暂存/同卷发布/受控清理与 M4b 套件/报告/登记；`adapters/llm/` 实现 A/B 参数数组子进程桥、固定三角色与固定诊断；`adapters/sqlite/` 实现 v1 五表/约束/索引、初始化核验、BEGIN IMMEDIATE 事务仓储与 primary 设备租约；`process_log.py` 追加固定字段的本机过程日志（含 `store/sense/read/policy/eval/decision/adb_hierarchy/adb_screen` 阶段）。M4c+ 模块尚未创建。
- `tests/`：M0–M4b 标准库 `unittest` 与纯合成夹具。`python -m unittest tests.test_doctor` 覆盖正反例、畸形 keyguard、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单行多组件/前后附加内容、重复 JSON 键、孤立代理字符、提示行、`output` 成功/失败、日志白名单/故障注入、ADB `started→结束` 与 fixture 零子进程断言；`tests.test_store` 覆盖合成快照入库、竞争、TTL、停止未知/异主机、三态收敛、blocked 设备隔离、旧 nonce/截止时间、clock_changed 与三类事务故障注入；`tests.test_migrations` 覆盖建库/幂等/回滚、schema 篡改与 `store init` CLI；`tests.test_sensor` 覆盖 M2 XML/profile/manifest、四源聚合、分页合并、过期/未标定、独立键算法与 stop_reason；`tests.test_sensor_store` 覆盖 sense 落库/重放/终态/仓储错误/日志关联/零设备网络进程；`tests.test_read` 覆盖 M3 页面/职位/未读/会话、字段阶梯、像素阈值/分辨率/损坏、稳定消息身份、跨屏合并、门禁与五类 CLI fixture；`tests.test_screen` 覆盖 mock ADB 参数/超时/不重试/证据保存与共享门禁；`tests.test_policy_contract` 覆盖 Envelope/Decision、摘要、限额与回复复验；`tests.test_policy_pack` 覆盖 TOML/三 md、45 组合、硬下限、渲染与 preview schema；`tests.test_policy_store` 覆盖 policy 六命令、路径/大小/编码/链接、快照/恢复三值、受控拒绝、四键外层与日志；`tests.test_bridge` 覆盖 A/B 真实参数数组子进程、三角色、一次启动重试、提交后超时与认证/额度/断连/角色/协议诊断；`tests.test_policy_eval` 覆盖实包加载、报告绑定、分桶、24 行映射、成功/失败凭证与留出冻结/访问/退役/零样本；`tests/m1_helpers.py` 提供假时钟与合成快照/查询辅助；`tests/policy_helpers.py` 提供临时项目根（含公开套件）与合成 base/包/决策辅助；`tests/fixtures/bridge/bridge_stub.py` 是合成本机桥桩。独立场景协调端维护的 `tests/scenarios/`、`tests/test_offline_scenarios.py` 与 `tests/private_regression/` 不在本 Task 范围。
- `pyproject.toml`：正式名称、版本 0.1.0、Python >=3.13、MIT 与空 Python 依赖；M0–M4b 入口在 `app/`，尚未声明发行包。
- `requirements.txt`：Python 标准库及无第三方依赖声明，不代表旧业务测试仍存在。
- `package.json`：评测工具元数据、Node 版本要求、固定 promptfoo 开发依赖和 npm test/eval:smoke/eval:model 入口。
- `package-lock.json`：npm 依赖完整锁文件，使用 npm ci 重现安装；依赖更新时同步维护。
- `promptfooconfig.json`：根评测配置，引用 evals 提示词、案例和断言；echo 为占位，不当作真实模型，默认关闭分享、并发 1。
- `evals/`：24 个合成黄金案例、单次决策提示词、断言、正反例自检、参考 provider 及 smoke/model 运行入口；另有 M4b `policy-suite/` 公开合成套件与旧 24 行责任映射，独立于 promptfoo 回归。
- `docs/`：公开文档目录。终稿 `BOSS直聘_手机客户端原始操作_原子化分析.md` 合并旧项目 `scripts/` 的静态阅读结果，按 CORE/JOBS/HRR/HRA 前缀的原子 ID 组织，含 0–9 小节与源码索引；技术结论带 `file:line` 证据，不构成设备执行授权。同目录另有 `BOSS直聘_实机验证偏差与修订建议_20261123.md`（单机型单版本，记录 7 条偏差与 5 条待确认）、`BOSS直聘_求职自动化_简化架构设计说明_20261123.md`（失败证据、简化架构、接口与验收指标）、`BOSS直聘_求职自动化_实施计划_20261123.md`（M0–M8 里程碑、六张图、测试与回滚）、`求职自动化_多应用扩展与分层_设计说明_20261123.md`（分层、端口、多应用共存与接入猎聘步骤）、`求职自动化_公开仓库与策略包_设计说明_20261123.md`（公开/私有资产边界、策略包与黄金集分层）、`求职自动化_新环境自动化部署方案_20261123.md`（新环境现状证据、依赖矩阵、拟议引导与部署入口、权限/恢复/配置保留、ADB 工具准备、手机检查及 A01–A08 独立验收、官方来源与干净机器验收矩阵；尚未实现安装器/检查器或验证初始环境/手机）、本目录 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`。
- `docs/BOSS直聘_求职自动化_场景测试方案_20261123.md`：M0–M8 场景规程与随机未读分支，区分现有自检、未来离线、受控设备与真实观察/写入；含独立判据、证据、指标和发布补测，只做方案不操作设备。
- `docs/BOSS直聘_场景测试方案_三轮自审记录_20261123.md`：场景方案三轮作者文档自审的发现、修订、复核与阶段提交证据，不代表实机或独立测试人员验收。
- `docs/BOSS直聘_M3读取阶梯与最小数据契约_20261124.md`：M3 离线读取阶梯与最小数据合同。定稿 `ReadResult` 值类型分派、页面/职位/未读/会话值类型、字段证据与决策门禁、跨屏稳定消息身份（内容指纹与稳定身份分离）、会话身份核对、像素标定与失败边界、`read` fixture/CLI 形状、采集适配器与场景责任；不改 M1 五表、不持久化正文，真实 run 在共享设备门禁前拒绝，不代表真机标定或只读一轮通过。
- `docs/operator.md`：M0–M4b 操作手册。说明 fixture 命令、最小配置、显式真实设备模式、设备确认要求、JSON 字段、退出码、夹具规则、`store init` 路径/幂等/schema/租约/错误码、`sense` 的 manifest/profile/XML/四源/分页/停因/落库规则、`read` 的页面/职位/未读/会话/字段阶梯/像素/跨屏身份/门禁、`policy` 的初始化/校验/预览/列表/快照/受控恢复/四键错误、M4b `policy eval` 的 provider/授权/桥超时/报告/凭证/留出生命周期与 `policy holdout`、本机过程日志路径/字段/复核方法与未实现范围。
- `prompts/`：M4a 公开 base、纯合成示例与 `policy init` 生成的本机策略包位置。`base/` 提供两份公开契约；`examples/default/` 是四载荷全 C 空事实示例；`local/` 与 `history/` 被忽略，只在 init/快照时生成，不作为活动源提交。M4b eval 实际加载 `--pack` 指定活动包，不读取 examples 冒充当前策略。
- `config.example.json`：示例配置，当前只含 `policy_pack`（指向被忽略的本机策略包）；其余 schema 待设计，当前不是 M0 可消费配置。
- `config.json`：当前存在、被忽略的本机配置，保留且不读取内容；不是评测配置。
- `.gitignore`：依赖、缓存、运行产物、个人事实及凭据忽略约定，包含 data/screenshots 指导文档例外。
- `.agents/`：Codex 工作流入口占位，当前只有双指导文档，Skills 待设计。
- `.claude/`：Claude 工作流入口占位，当前只有双指导文档，Skills 待设计。
- `data/`：本机产物目录，含自动生成的 evals 结果/状态子目录、M4b `data/evals/eval-<run-id>/` 报告/凭证与 `holdout-registry.local.json`、`logs/m0-process-<运行 UUID>.jsonl` 每次运行独立过程日志与 `runtime/` M1/M2 SQLite 库；M3 read 不写业务库，M4a policy 只写被忽略的 `prompts/local/` 与 history，M4b eval 只写被忽略的 `data/evals`，采集证据由测试临时目录或被忽略本机路径使用。运行产物排除文档覆盖且不提交 Git。过程日志只含固定字段（含 `store/sense/read/policy/eval/decision/adb_hierarchy/adb_screen` 阶段），写入失败不影响 stdout/退出码，但 stderr 固定标记 `m0_process_log_degraded`，不声称完整日志已保存。
- `screenshots/`：本机截图预留位置，当前只含双指导文档，截图不提交 Git。
- `AGENTS.md`：项目事实、完整索引、母本规则原文及命令状态；子目录指南引用本文件。
- `CLAUDE.md`：仅含 `@AGENTS.md`，必须与同目录指南配合使用，不能另存一套规则。
- `.git/`：Git 元数据，排除指导文档覆盖。
- `.venv/`：本机 Python 虚拟环境，被忽略且排除指导文档覆盖。
- `node_modules/`：npm 自动安装的第三方评测依赖，被忽略且排除指导文档覆盖。

`scripts/`、`skills/`、`.agents/skills/`、`.claude/skills/` 当前不存在，按规则不创建；Node 回归测试实际放在 evals，M0–M4b Python 回归测试放在 tests。旧 scripts/main.py 与旧业务 unittest 约定不适用当前仓库。`docs/` 当前已存在，存放公开分析文档、M0–M4b 操作手册、M3 契约与目录指南（见根目录索引），不承载业务代码或测试。`prompts/local/`、`prompts/local/history/` 与 `evals/private/` 被忽略且不预建；`prompts/base/`、`prompts/examples/default/`、`tests/fixtures/policy-preview/` 与 `evals/policy-suite/` 是已交付公开资源，preview/eval 依赖且部署必须携带。`.promptfoo/`、`build/`、`dist/`、`.test-artifacts/`、`test-evidence/` 当前不存在，按规则不创建；未来产生的依赖、缓存和自动生成产物也不补指导文档。

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

以下均在项目根执行。M0–M4b 使用 Python >=3.13 标准库；评测需 Node.js >=22.22.0 与 npm，依赖以 package-lock.json 为准：

```powershell
python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen tests.test_policy_contract tests.test_policy_pack tests.test_policy_store tests.test_bridge tests.test_policy_eval
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
Get-ChildItem data/logs/m0-process-*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
git check-ignore data/logs/m0-process.jsonl data/runtime/m1-acceptance.sqlite3 data/runtime/m2-acceptance.sqlite3 prompts/local/demo prompts/local/history data/evals/eval-deadbeef/report.json
npm ci
npm test
npm run eval:smoke
npm run eval:model -- "<provider-id>"
```

M4b `policy eval` 的离线正路径需要显式本机桥程序与 `--confirm-external-send`；本 Task 不提供真实 provider 命令，也不实际外发。`policy eval` 失败/零样本/非公开/simulated 不生成验收凭证。

`tests.test_doctor` 覆盖 doctor 正反例、重复 JSON 键、孤立代理字符、畸形 keyguard token、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单 Window 多组件/前后附加内容、提示行清单、设备失败七键映射、stdout 写失败与 `output=failed`、帮助 output/exit、日志白名单/降级故障注入、ADB `started→ok/failed`/中断与 fixture 零子进程断言。ready 夹具命令在日志正常时退出 0、只输出一个 JSON、stderr 为空且不写业务库；每次运行只创建自己的 `data/logs/m0-process-<运行 UUID>.jsonl`，不向共享文件追加。日志写入失败时 stdout/退出码不变，stderr 仅写固定 `m0_process_log_degraded`，表示日志不完整，不能声称完整日志已保存。fixture 模式零设备、零网络、零子进程；真实 `doctor --device --serial <serial>` 只在取得本轮设备确认后手动执行，离线验收不运行。

`tests.test_process_log_concurrency` 用两个独立 spawn 进程与 Barrier，分别覆盖默认生产目录和显式测试基础路径：两运行必须写不同 UUID 文件，各自按 correlation_id 得到完整、有效、无混行的固定事件序列；显式基础路径不被写入，避免共享追加。`tests.test_store` 覆盖同一候选竞争、不同 owner 全局设备互斥、仅 TTL 过期不释放、priority/created/key 领取顺序、start/renew/finish/recover、blocked 的任务清租约与设备原身份隔离、ReadResult.undetermined/Evidence.unknown 两条路径、旧 nonce/旧截止时间/重复操作、停止未知/异主机/未确认、clock_changed、事件与任务/设备更新未命中/事件写入失败的整体回滚、伪造会话键原子回滚，以及非法类型/枚举、非 UUID owner、跨 app 租约键与 bool 类型；Windows 停止检查器只用 mock ctypes，不读取真实 PID。`tests.test_migrations` 覆盖 v1 STRICT 五表/索引、重复初始化不改业务行、建表中途及版本写入后回滚、初始化写锁 `store_busy` 与释放后幂等、未知版本/非空无版本/schema 篡改拒绝、外键与 check 约束、非 SQLite 文件，以及 `store init` 固定 JSON、默认路径、路径越界、损坏/篡改库和失败日志。`python -m app store init --db data/runtime/m1-acceptance.sqlite3` 连续两次退出 0、只输出固定成功 JSON，第二次只核验且不改业务行；数据库写在被忽略的 `data/runtime/`。

`tests.test_sensor` 覆盖 M2 合成 profile/manifest、正例独立键算法、区域角标边界与行字段排除、区域巨大整数、坏数字节点几何优先、超大行坐标不逃异常、四源正/零/未知冲突、全局标签语义、预览基线、身份缺失/重复、分页最长整行重叠、成功页 null snapshot、完整遍历/全局零覆盖、过期/未来快照、未标定版本、非法 XML/DTD/ENTITY/Unicode/嵌套 hierarchy、manifest/profile 重复 JSON 键、路径越界/链接、非法基线摘要与有序 stop_reason。`tests.test_sensor_store` 覆盖 `sense` 正例两次执行、发现总数 1 且重放不追加事件、唯一 unread 候选、终态同指纹不重开、终态旧键与不同 queued 新键、四类停止原因、未标定/坏输入/非法基线/巨大区域/非法 Unicode 不创建业务库、parse 三态日志、聚合/停因成功与内部失败轨迹、日志降级、超大坐标 CLI 固定 JSON、仓储错误三键安全出口、严格参数、同一日志实例/correlation 与零设备/网络/子进程/任务租约写入口断言。`python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3` 连续两次退出 0，输出 ok/present、唯一 unread、stop_reason=null，重放发现仍 1 且无额外事件；数据库写在被忽略的 `data/runtime/`。

`tests.test_read` 覆盖 M3 五类 `read` CLI fixture、页面分类/unknown 门禁、职位卡规范化/重复身份/详情截断、未读行身份与像素后备、阈值/分辨率/损坏/未知格式、字段阶梯前级优先/冲突/failed/视觉默认关闭/人工、会话身份/方向/稳定消息键/跨屏重叠/新增消息/过期、ReadResult 值类型分派、M1 仓储拒绝非 UnreadSnapshot、定向核对与 undetermined 门禁；`tests.test_screen` 只 mock `subprocess.run`，覆盖 dump/cat/screencap 参数、`shell=False`、5 秒超时、不重试、证据保存/失败、共享门禁先于采集。`python -m app read --fixture tests/fixtures/read/conversation.json` 在当前合成输入下退出 0、`actionable=true`、`inbound_state=known`；页面/职位/未读 fixture 同样接生产解析与阶梯逻辑，不写业务库。

`tests.test_policy_contract` 覆盖 Envelope/Decision 键/类型/词表/回显/证据/nonce 租约参数/2048 字节边界、嵌套重复 JSON 键、H 无规范化与四类摘要无自引用、64 KiB 渲染上限、职位五个 intent、围栏拒绝、披露追加后的码点/控制字符/联系方式/禁谈/事实复验；`tests.test_policy_pack` 覆盖 TOML+三 md 完整 schema、45 组合唯一覆盖、同档重复/跨档冲突/漏项/A 越权、硬下限、有效限制、联系方式、system/developer/user 投影与 preview 案例 schema；`tests.test_policy_store` 覆盖路径/普通文件/链接/BOM/NUL/大小/CRLF、`policy init/validate/preview/list/snapshot`、编辑语气/分档/披露、清单与复制摘要、restore 三值/base/清单/快照/目标篡改、生产受控拒绝、合成 stopped 正路径、失败清理、四键外层、参数错误区分、日志 stage/operation/结果与零设备/网络/子进程。`policy` 只用临时项目根与固定合成 base/包/preview，不写仓库 `prompts/local`；生产 restore 固定拒绝，正路径仅在测试向同一用例注入明确停止证据。

`tests.test_bridge` 覆盖 A/B 真实参数数组子进程成功、固定 system/developer/user 三消息、无工具无历史、伪造 role 保持 user 数据、真实启动失败一次重试后固定失败、提交后 20 秒超时不重试、认证/额度/断连/坏 JSON/重复键/角色不符/坏回显固定诊断、未确认事实回复拒绝、包变动与桥前后租约过期、RenderedRequest 篡改先拒绝。`tests.test_policy_eval` 覆盖实际加载当前包、公开套件模拟与真实适配器路径报告、base/pack/profile/provider/model/model_config_hash/suite_hash 绑定、模拟与失败无凭证、成功凭证、授权拒绝前零桥调用、编辑 pack 改变 developer/prompt_hash/pack_hash、24 行映射与旧 24 文件不变、公开/回归/留出分桶、留出冻结/访问/同摘要重跑非新证据/摘要变化拒绝/退役/新批次/外部 provider 拒绝/零样本未测、CLI eval help/echo/未确认与 holdout register/retire。所有决策/回复均为合成与合成本机桥桩；真实 provider、私有留出与 M4c 真实租约未运行。

npm ci 安装锁定依赖；npm test 运行 Node 内置正反例检查，无需安装 promptfoo；smoke 需先安装依赖，仅回放合成参考答案，不请求模型。model 必须指定真实 provider 并按其类型配置认证，可能产生调用费用；本次未选择或调用实际模型。M4b `policy eval` 是独立 Python 入口，只接收显式 A/B 本机桥程序；离线测试注入模拟 transport 仍走同一授权回调，simulated 不生成凭证。动作、调度与 run 测试或独立构建/lint 命令尚未实现，不执行旧 scripts/main.py/unittest 命令。

本次已执行 `python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen`（227 个测试，含子测试断言；退出 0）、`python -m app doctor --fixture tests/fixtures/doctor/ready.json`（退出 0）、`python -m app store init --db data/runtime/m3-regression.sqlite3`（退出 0、固定 JSON）、`python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m3-regression.sqlite3`（退出 0）、`python -m app read --fixture tests/fixtures/read/{page,job_list,job_detail,unread,conversation}.json`（均退出 0、接生产解析/阶梯、零设备/库/子进程）、`npm test`（52 项通过）；主端独立脚本串行执行为阶梯 11/11、身份 15/15、CLI/像素 27/27、CLI 负例 5/5（并行运行会因共享默认日志目录产生额外 created 文件，不作为产品缺陷）。另完成工作树 `git diff --check`、基线 `git diff --check cc103f8bd479ccecead33af71ffd1a429e1cbd4a <新SHA>`、`compileall` 与敏感路径静态检查。M3 测试覆盖页面/职位/未读/会话、字段阶梯来源槽位/前级优先/冲突/failed/视觉默认关闭/人工/跨上下文自校验/派生结果篡改/保留冲突或失败证据伪造拒绝、会话身份与方向/重复正文与方向控件/稳定消息键/重复 ID 会话未知/后页重复跨页传播/跨屏重叠/新增消息同会话核对/过期、公开解析入口标定/context/合并值类型/profile 校验、不支持标定边界零 pixel 事件/实际像素 started→failed、错页零 pixel 日志、像素阈值/分辨率/损坏/未知格式/显式零/像素 count/日期/来源/colorspace、undetermined/identity/gate/证据保存失败、定向核对重放/陈旧/错 profile、序列化唯一归属、ReadResult 值类型分派、M1 仓储拒绝非 UnreadSnapshot、共享门禁先于采集与 mock ADB 参数/超时/不重试。它们验证 M0 判据、M1 合成库/租约/回滚、M2 合成解析/落库/停因、M3 离线读取与评测链路，不代表真实模型、真实设备、运行时调度、真机像素/稳定标识标定或真机只读一轮。新增修改后须运行受影响检查，不能沿用该记录当作新版本验证。

M4a 返修后已执行 `python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen tests.test_policy_contract tests.test_policy_pack tests.test_policy_store`（343 个测试，125.717s，退出 0；M4a 116 项）、`npm test`（52 项通过）、`python -m compileall -q app tests`、`git diff --check` 与 `git check-ignore`；另在合法 CRLF 的独立副本运行同一 343 项（123.672s）全部通过；以基线 `584b81495c8cb988a8722b082cca9da0181f3c7a` 的 `HEAD:AGENTS.md` 提取 `RULES:BEGIN/END` 区与工作树逐字比较，均为 2406 字符且一致，未改全局规则。另在临时项目根实际执行 `policy init` → 编辑 tone/披露 → `validate` → `preview` → `list` → `snapshot` → 同一生产 `restore_pack` 注入明确合成 `stopped` → `validate` → `preview`，均 ok 且恢复 pack_hash 与清单一致；生产 CLI `restore` 无可信停止证据返回 `policy_changed` 并记 `undetermined`。M4a 测试覆盖 Envelope/Decision 严格键/类型/词表/回显/证据/租约参数/2048 字节、嵌套重复 JSON 键、四类摘要无自引用与 H 无规范化、唯一 {base,pack,data} 内容对象 64 KiB 边界、TOML+三 md 完整 schema、45 组合覆盖/重叠/漏项、2 A/4 B/39 C 统一硬下限、路径/链接/BOM/NUL/大小/控制字符/联系方式（含可选双指南）、包内事实模式与回复事实值严格匹配（保留小数/范围/单位/地点全文，经验须明确单位、普通数量问句不误判、未知数量级受控 conflict）、subject 次要空字符串、披露追加与禁谈复验、system/developer/user 投影、事实交集与重复/冲突、init/validate/preview/list/snapshot/restore 三值、复制前后源重读、清单/快照/目标篡改、失败清理与不覆盖、生产受控拒绝、逻辑停止正路径、四键/配置错误外层、链接候选 list、load/validate/render/publish 阶段日志与零设备/网络/子进程。该记录不代表 M4b/c 真实桥/评测/运行绑定、真实模型、真实设备、事实确认入口、共享门禁或发送授权已验收。

M4b 本轮已执行 `python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen tests.test_policy_contract tests.test_policy_pack tests.test_policy_store tests.test_bridge tests.test_policy_eval`（370 个测试，70.678s，退出 0；新增 M4b 27 项）、`npm test`（52 项通过）、`npm run eval:smoke`（24/24 参考回放，仅旧链路）、`python -m compileall -q app tests`、`git diff --check`、`git check-ignore` 与敏感路径静态检查；旧 24 案例/参考回放未改，reference smoke 只证明旧评测链路。M4b 测试覆盖真实 A/B 参数数组子进程、固定三角色、伪造 role 保持 user 数据、启动失败一次重试、提交后 20 秒超时不重试、认证/额度/断连/坏 JSON/重复键/角色不符/坏回显固定诊断、事实复验、包变动/租约过期/RenderedRequest 篡改、实包 eval 报告摘要绑定、模拟/失败无凭证、成功凭证、授权拒绝零桥调用、编辑 pack 改变实际请求与摘要、24 行映射、公开/回归/留出分桶、留出冻结/访问/退役/零样本与 CLI eval/holdout。该记录不代表真实 provider、私有回归/留出、M4c 真实活动/事实确认、M5 授权/动作或真机已验收；未实际模型外发。新增修改后须运行受影响检查，不能沿用该记录当作新版本验证。

文档维护的一次性校验仍按初始化提示词：比较母本规则区两个标记与全文、8 小节顺序、Python/TypeScript 各 2 个示例，并检查自有目录的双指南、精确大小写、引用与直接子项索引；排除 Git、虚拟环境、node_modules 和 data/evals 等自动生成产物。此类校验不是业务测试。

M0–M4b 已完成离线实现；M3 真机标定与只读一轮、M4c 真实活动/事实确认、M5+ 动作/调度均未测。后续仍需按里程碑补充真实运行绑定、业务范围与授权模型、运行时决策记忆、事实确认入口、业务配置 schema、设备适配及真实业务验证。确定后同步更新各受影响指南。
