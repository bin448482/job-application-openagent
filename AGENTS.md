# job-application-openagent 项目指南

## 一、项目基本信息

正式名称为 `job-application-openagent`，依据 `README.md` 与 `pyproject.toml` 的 `project.name`。根目录为本指南所在目录，运行路径相对于项目根目录解析。

本项目面向开源维护的 Android 求职辅助工作流，目前处于分阶段实现阶段。M0 只读探针与本机白名单过程日志已实现；旧业务源码、Skills 与业务测试已清理，不恢复旧实现或个人授权。当前已有独立的 promptfoo 合成黄金集，检查记忆与授权决策；M1+ 的业务 CLI 扩展、SQLite 运行时记忆、设备执行器和完整业务范围仍待设计。

本仓库自身：业务项目要求 Python >=3.13，Python 依赖保持为空；评测开发工具另需 Node.js >=22.22.0、npm 和固定版本 promptfoo 0.126.0。评测使用 CommonJS、Node 内置测试和 JSON 配置，npm 依赖通过 package-lock.json 锁定。引入 promptfoo 是当前明确的评测需求，不改变 Python 标准库优先约束。

文档所描述的消费方：评测可接受 promptfoo provider ID 连接模型或自定义端点，认证由使用者按 provider 类型配置，不提供默认真实模型。README 约定未来设备功能需要 Android SDK Platform-Tools 的 adb、USB 调试及使用者确认；当前没有业务 Skill 或设备工作流，不推定宿主、机型、包名或发送授权。

主要入口与模块关系：README 提供使用概览；`python -m app` 进入 app/cli/main.py，M0 只分派只读 doctor，fixture 由 CLI 读取，真实探针经 app/ports/device.py 注入 app/adapters/adb/device.py，业务判定在 app/usecases/doctor.py，白名单过程日志由 app/process_log.py 追加到 data/logs/m0-process.jsonl。package.json 的 npm scripts 调用 evals 的断言测试或评测入口，promptfooconfig.json 加载提示词、24 个案例和 JavaScript 断言。smoke 仅回放参考输出，model 才使用显式 provider；case metadata 不渲染进模型提示词。评测产物仅保存到 data/evals。pyproject.toml 为业务元数据，不是已实现的 CLI 或发行包。每个目录的 CLAUDE.md 仅引用同目录 AGENTS.md，全局规则只在根指南保存。

新环境自动化部署方案已形成设计文档，建议首批 Windows 11 x64 / Windows PowerShell 5.1，分别验收 Node 合成评测、ADB 工具准备和经本轮确认后的手机环境检测；Python venv、模型认证、Android 与开发编排工具不作为当前评测必需。Android 检测不等待完整业务，但须先实现检查器与共享设备门禁；部署入口、引导发行物与检查器尚未实现，干净机器及手机验收均待执行，本轮仅文档、未运行 ADB；既有本机测试记录不能替代该验收。

项目特有约束与已知限制：

- 默认使用标准库，仅在真实需要时添加依赖；评测依赖按 npm 开发依赖管理。源代码顶部说明用途，函数和方法写 docstring/JSDoc；修改模块时同步目录指南。
- 源码、环境与数据库只依赖当前独立项目，不依赖原项目虚拟环境、数据库或仓库外业务规则母本；当前不进行旧个人策略通用化，不恢复已清理业务代码与 Skills。
- 不扩展真实自动发送授权，不继承旧工作地点、简历事实或联系方式授权。黄金集中的城市、薪资、记忆、授权与 HR 消息均为合成输入；can_send=true 只是拟执行测试决策，不触发真实发送、记忆写入或手机动作。
- 数据库、日志、契约、截图、凭据和个人事实只在本机保存。不读取或记录密钥、令牌、密码等敏感值；不读取根 config.json 或私人事实来填充测试集。新增故障案例须先脱敏并确定独立判据，不直接把模型输出当正确答案。
- 操作手机前说明具体动作、预计占用时长和是否独占，并取得本轮确认；未确认不得连接设备或运行 ADB。npm 自检和黄金集评测均不操作设备。
- config.example.json 当前只含 `policy_pack`，指向被忽略的本机策略包；其余业务 schema、字段和默认值仍待设计。M0 最小配置规则已在 CLI 实现（只读模式两项键），不写入本示例文件。config.json 是被忽略的本机文件，不作为示例或授权来源，不沿用旧批次大小、每日额度或 HR 间隔。
- promptfooconfig.json 的默认 provider 为 echo 占位，sharing=false、并发上限 1；通过 `npm run eval:smoke` 才回放参考 provider。`npm run eval:model -- "<provider-id>"` 必须指定 provider，拒绝 echo 和内置参考 provider；真实评测将合成输入发往所选服务，认证与计费按该 provider 配置。
- evals/run.cjs 设置 PROMPTFOO_DISABLE_TELEMETRY=1、PROMPTFOO_DISABLE_UPDATE=1，配置 PROMPTFOO_CONFIG_DIR 为本项目 data/evals/promptfoo、PROMPTFOO_CACHE_PATH 为 data/evals/cache；通过 --no-share、--no-cache、--no-write 关闭分享、缓存和评测数据库保存。逐次 results.json 写入 data/evals/smoke-UUID 或 model-UUID。直接使用裸 promptfoo 命令不会自动继承本入口的环境设置。
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

- `README.md`：当前范围、M0 只读探针命令、业务和评测环境、npm 安装/自检/模型评测命令、数据边界与未实现部分；可独立阅读的使用概览，不是全局规则母本。
- `LICENSE`：MIT 许可证，2026 年项目贡献者署名。
- `app/`：M0 分层程序包。`__main__.py` 提供 `python -m app`；`cli/` 负责严格参数、配置、路径与 JSON 输出，并在共享 JSON 边界拒绝重复对象键；`ports/` 声明设备探针端口；`usecases/` 做输入校验、UTF-8 文本约束与唯一 doctor 结果组装；`adapters/adb/` 实现仅显式触发、只接受 tab 分隔确定清单记录的真实 ADB 探针；`process_log.py` 追加固定字段的本机过程日志。M1+ 模块尚未创建。
- `tests/`：M0 标准库 `unittest` 与纯合成 doctor 夹具；`python -m unittest tests.test_doctor` 覆盖正反例、畸形 keyguard、整条 focus 完整 Window 匹配/单行多组件/前后附加内容、重复 JSON 键、孤立代理字符、提示行、日志白名单/故障注入、窗口解析与 fixture 零子进程断言。独立场景协调端维护的 `tests/scenarios/` 与 `tests/test_offline_scenarios.py` 不在本 Task 范围。
- `pyproject.toml`：正式名称、版本 0.1.0、Python >=3.13、MIT 与空 Python 依赖；M0 入口在 `app/`，尚未声明发行包。
- `requirements.txt`：Python 标准库及无第三方依赖声明，不代表旧业务测试仍存在。
- `package.json`：评测工具元数据、Node 版本要求、固定 promptfoo 开发依赖和 npm test/eval:smoke/eval:model 入口。
- `package-lock.json`：npm 依赖完整锁文件，使用 npm ci 重现安装；依赖更新时同步维护。
- `promptfooconfig.json`：根评测配置，引用 evals 提示词、案例和断言；echo 为占位，不当作真实模型，默认关闭分享、并发 1。
- `evals/`：24 个合成黄金案例、单次决策提示词、断言、正反例自检、参考 provider 及 smoke/model 运行入口。
- `docs/`：公开文档目录。终稿 `BOSS直聘_手机客户端原始操作_原子化分析.md` 合并旧项目 `scripts/` 的静态阅读结果，按 CORE/JOBS/HRR/HRA 前缀的原子 ID 组织，含 0–9 小节与源码索引；技术结论带 `file:line` 证据，不构成设备执行授权。同目录另有 `BOSS直聘_实机验证偏差与修订建议_20261123.md`（单机型单版本，记录 7 条偏差与 5 条待确认）、`BOSS直聘_求职自动化_简化架构设计说明_20261123.md`（失败证据、简化架构、接口与验收指标）、`BOSS直聘_求职自动化_实施计划_20261123.md`（M0–M8 里程碑、六张图、测试与回滚）、`求职自动化_多应用扩展与分层_设计说明_20261123.md`（分层、端口、多应用共存与接入猎聘步骤）、`求职自动化_公开仓库与策略包_设计说明_20261123.md`（公开/私有资产边界、策略包与黄金集分层）、`求职自动化_新环境自动化部署方案_20261123.md`（新环境现状证据、依赖矩阵、拟议引导与部署入口、权限/恢复/配置保留、ADB 工具准备、手机检查及 A01–A08 独立验收、官方来源与干净机器验收矩阵；尚未实现安装器/检查器或验证初始环境/手机）、本目录 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`。
- `docs/BOSS直聘_求职自动化_场景测试方案_20261123.md`：M0–M8 场景规程与随机未读分支，区分现有自检、未来离线、受控设备与真实观察/写入；含独立判据、证据、指标和发布补测，只做方案不操作设备。
- `docs/BOSS直聘_场景测试方案_三轮自审记录_20261123.md`：场景方案三轮作者文档自审的发现、修订、复核与阶段提交证据，不代表实机或独立测试人员验收。
- `docs/operator.md`：M0 操作手册。说明 fixture 命令、最小配置、显式真实设备模式、设备确认要求、JSON 字段、退出码、夹具规则、本机过程日志路径/字段/复核方法与未实现范围。
- `config.example.json`：示例配置，当前只含 `policy_pack`（指向被忽略的本机策略包）；其余 schema 待设计，当前不是 M0 可消费配置。
- `config.json`：当前存在、被忽略的本机配置，保留且不读取内容；不是评测配置。
- `.gitignore`：依赖、缓存、运行产物、个人事实及凭据忽略约定，包含 data/screenshots 指导文档例外。
- `.agents/`：Codex 工作流入口占位，当前只有双指导文档，Skills 待设计。
- `.claude/`：Claude 工作流入口占位，当前只有双指导文档，Skills 待设计。
- `data/`：本机产物目录，含自动生成的 evals 结果/状态子目录与 `logs/m0-process.jsonl` 过程日志；运行产物排除文档覆盖且不提交 Git。过程日志只含固定字段，写入失败不影响 stdout/退出码，但 stderr 固定标记 `m0_process_log_degraded`，不声称完整日志已保存。
- `screenshots/`：本机截图预留位置，当前只含双指导文档，截图不提交 Git。
- `AGENTS.md`：项目事实、完整索引、母本规则原文及命令状态；子目录指南引用本文件。
- `CLAUDE.md`：仅含 `@AGENTS.md`，必须与同目录指南配合使用，不能另存一套规则。
- `.git/`：Git 元数据，排除指导文档覆盖。
- `.venv/`：本机 Python 虚拟环境，被忽略且排除指导文档覆盖。
- `node_modules/`：npm 自动安装的第三方评测依赖，被忽略且排除指导文档覆盖。

`scripts/`、`skills/`、`.agents/skills/`、`.claude/skills/` 当前不存在，按规则不创建；Node 回归测试实际放在 evals，M0 Python 回归测试放在 tests。旧 scripts/main.py 与旧业务 unittest 约定不适用当前仓库。`docs/` 当前已存在，存放公开分析文档、M0 操作手册与目录指南（见根目录索引），不承载业务代码或测试。`.promptfoo/`、`build/`、`dist/`、`.test-artifacts/`、`test-evidence/` 当前不存在，按规则不创建；未来产生的依赖、缓存和自动生成产物也不补指导文档。

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

以下均在项目根执行。M0 使用 Python >=3.13 标准库；评测需 Node.js >=22.22.0 与 npm，依赖以 package-lock.json 为准：

```powershell
python -m unittest tests.test_doctor
python -m app doctor --fixture tests/fixtures/doctor/ready.json
Get-Content data/logs/m0-process.jsonl -Tail 20
git check-ignore data/logs/m0-process.jsonl
npm ci
npm test
npm run eval:smoke
npm run eval:model -- "<provider-id>"
```

`tests.test_doctor` 覆盖 doctor 正反例、重复 JSON 键、孤立代理字符、畸形 keyguard token、整条 focus 完整 Window 匹配/单 Window 多组件/前后附加内容、提示行清单、设备失败七键映射、stdout 写失败、日志白名单/降级故障注入与 fixture 零子进程断言。ready 夹具命令在日志正常时退出 0、只输出一个 JSON、stderr 为空且不写业务库；每次运行只追加被忽略的 `data/logs/m0-process.jsonl`。日志写入失败时 stdout/退出码不变，stderr 仅写固定 `m0_process_log_degraded`，表示日志不完整，不能声称完整日志已保存。fixture 模式零设备、零网络、零子进程；真实 `doctor --device --serial <serial>` 只在取得本轮设备确认后手动执行，离线验收不运行。npm ci 安装锁定依赖；npm test 运行 Node 内置正反例检查，无需安装 promptfoo；smoke 需先安装依赖，仅回放合成参考答案，不请求模型。model 必须指定真实 provider 并按其类型配置认证，可能产生调用费用；本次未选择或调用实际模型。M1+ 的业务 Python CLI、业务测试或独立构建/lint 命令尚未实现，不执行旧 scripts/main.py/unittest 命令。

本次已执行 `python -m unittest tests.test_doctor`（30 个测试方法，含子测试，退出 0）、`python -m app doctor --fixture tests/fixtures/doctor/ready.json`（退出 0，stderr 为空，未新增业务库，日志追加成功）、`npm test`（52 项通过）与 `git diff --check`；另完成根 `AGENTS.md` RULES 区逐字比对。此前另行执行 npm install --no-fund --no-audit 生成锁文件并安装评测依赖；smoke 的 24 个案例回放通过。它们只验证 M0 判据、参数/配置/夹具负例、本机日志行为及评测链路，不代表真实模型、运行时记忆或真实设备验证。新增修改后须运行受影响检查，不能沿用该记录当作新版本验证。

文档维护的一次性校验仍按初始化提示词：比较母本规则区两个标记与全文、8 小节顺序、Python/TypeScript 各 2 个示例，并检查自有目录的双指南、精确大小写、引用与直接子项索引；排除 Git、虚拟环境、node_modules 和 data/evals 等自动生成产物。此类校验不是业务测试。

M0 已完成；后续仍需按里程碑补充业务范围与授权模型、运行时记忆和存储接口、业务配置 schema、设备适配及真实业务验证。确定后同步更新各受影响指南。
