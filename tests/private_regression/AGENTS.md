# tests/private_regression 目录指南

## 一、目录职责

保存旧项目私有回归补充工具：受控发现 UI XML 与失败日志，按白名单生成纯合成派生样本、失败窗口、manifest、provenance 与安全回执；并提供本机校验器与纯合成自测。它只读源项目、不执行旧代码、不连接设备、不调用网络/模型，不替代合成黄金集或证明业务 PASS。全局工作规则见[根目录 AGENTS.md](../../AGENTS.md)。

本目录是独立任务边界；`tests/` 的共享索引条目由协调端/业务方串行维护，本目录不修改 `tests/AGENTS.md` 或根指南。

## 二、当前结构

- `extract.py`：标准库工具（当前工具版本 `0.6.0`）。受控发现、路径/链接/重叠/覆盖防护、XML 白名单重构与异常合成代表、日志固定类别时序与失败窗口、安全回执、provenance、本机校验器。Git 输出边界查询只接受 `0/1` 正常语义，`2/128/负码/OSError` 固定 `git_unavailable`，不把查询失败当安全通过。预算命中（单文件/深度/节点/样本/扫描）会进入 `xml.truncated`/总 `truncated` 并给出 `partial`；读取期间 XML 或日志大小/mtime 变化、日志边界/链接失败按源改变 `error`。验证器用 `contextlib.closing(os.scandir(...))` 流式枚举批次条目；发现子目录时先检查 `depth+1`，超深在入集合/栈前立即停止并关闭迭代器；文件/目录上限同样逐条立即停止。拒绝未知文件/空目录/链接、解析前拒绝 DTD/实体/其他声明/注释/CDATA/PI，并重算状态与安全语义；有输出但无 XML 覆盖（如仅日志窗口）为 `partial`，`log_missing`/`no_failure_evidence` 为允许 `ok` 的软观察原因。
- `test_private_regression.py`：87 项 `unittest` 纯合成自测（上一轮 82 项判据全部保留 + 本轮 Git 故障安全新增 5 项），覆盖拓扑递归、属性/文本白名单、DTD/实体/编码/超限/深度/节点异常、注释/处理指令、Unicode 元素名解析前预算与 Unicode 属性值、路径逃逸、junction 祖先与子目录、输出重叠/覆盖/未忽略、源不变与 XML/日志读取期间增长/缩短/替换、日志 pre-open 复核、去重/抽样、provenance 对齐与绝对根行为、时序感知去重、长簇分片与尾部类别、物理行上限、NaN/Infinity、固定时序类别、缺时钟/乱序/截断、旧 oracle 不可信、stdout/异常泄露、只含 `needs_label`、XML 字符正文、未知自由字段、验证器拒绝重复 manifest/代表、未知文件/空目录/链接、文件与目录总量预算、流式枚举实际消费数/close/单目录超限/目录超限/深度 4-5/超深首项即停与同级零消费、真实 Git 坏索引初检/结束/validate/正控、Git 返回码矩阵与 OSError、状态组合矩阵、矛盾 status 与坏产物。
- `README.md`：公开使用说明、预算、状态/覆盖原因/安全语义、输出 schema、固定类别与限制。
- `SELF_REVIEW.md`：R1 数据边界/脱敏、R2 故障输入/抽样限制/独立 expected、R3 较早版本真实执行的作者声明、上一轮整改 R4–R6，以及本轮复审返修 R7–R9 作者自审；旧 R3 明确未在新实现重跑、不能为当前实现盖章，各轮记录问题矩阵、修订、合成复验与未测边界。
- `AGENTS.md`：本文件，目录索引与维护要求。
- `CLAUDE.md`：只包含 `@AGENTS.md`，引用本指南。

## 三、入口与接口

在项目根执行：

```powershell
python -m unittest discover -s tests/private_regression -p "test_*.py"
python tests/private_regression/extract.py --source "<source-root>" --output "<main-repo>/data/private-regression"
python tests/private_regression/extract.py --validate "<main-repo>/data/private-regression/<batch-id>"
```

`extract.py` 只接受显式 `--source`/`--output`（或 `--validate`），不自动寻找 `config.json` 或私人配置。实际输出根必须是 Git 忽略且未跟踪；每个批次目录名是随机 `b-<hex>`，不覆盖旧批次。

安全回执固定 schema：`schema`、`tool_name`、`tool_version`、`batch_id`、`status`、`discovery`、`xml`、`log`、`budget`、`safety`（错误时另有固定 `error_reason`）。stdout 只输出该回执或校验结果，不输出源文件名/路径、原始哈希、真实日期、可关联源 ID、派生正文或 traceback。

## 四、适用约束

- 真实源只读；不读取旧项目根 `AGENTS.md`、数据库、契约、配置、凭据、私人事实或 PNG；不执行 ADB/设备/网络/模型。
- 原始与真实派生正文、provenance 只保存在本机 Git 忽略目录；公开发布只含工具、合成测试与说明。
- XML 文本统一合成标记，resource-id 随机合成别名保留重复关系，package 固定，未知类退化为固定 View；未知属性丢弃。
- 异常 XML 只生成同故障类别的纯合成代表，不复制原始片段。
- 日志只提取固定事件/原因/状态/动作类别、重映射序号、相对时间和有界数值；绝对日期、原始事件 ID 和任意源字符串不进入样本或回执。
- 所有 `expected` 固定 `needs_label`；旧日志结果不作为 oracle。
- 发现/读取/校验全程有界；命中上限在回执标记 `truncated`/`partial`，不静默继续。XML 单文件/深度/节点命中保持真实故障计数，同时置 `xml.truncated=true` 并给出 `partial`；发现阶段已知超限不误报源改变。
- 源在抽取期间大小、mtime 或读取窗口变化，以及日志边界/链接复核失败时，工具以 `error`/退出码 2 结束；日志在发现阶段保存 stat，并在 pre-open、读取后、结束复核；结束复核覆盖全部已发现候选（未读候选仅 stat 范围）与已读 XML/完整日志的哈希，不能把未复核候选、读取失败候选或发现后被替换的日志当成已证明未变。
- provenance 只写本机 Git 忽略批次，`not_for_model=true`，实际保存绝对 `source_root` 与选中样本相对路径/大小/SHA-256；该绝对根不进入 stdout、回执或 manifest，公开文档不写实际值。
- 输出 Git 边界查询 `check-ignore -q` 与 `ls-files --error-unmatch` 只接受 `0/1` 正常语义；`2/128/负返回码/OSError` 固定 `git_unavailable`，不得映射成 `output_not_tracked=true`。初检失败不创建输出；结束复核失败 CLI 退出 2 且只输出固定错误回执，已写 XML/窗口可保留但不产生成功回执，不声称回滚；validate 返回 invalid/退出 2。原始 Git 错误与路径不进入 stdout/stderr/回执。
- 本机验证器以流式 `os.scandir` 枚举全批次目录与文件（文件 ≤256、子目录 ≤16、深度 ≤4），逐个条目即时检查；发现子目录时先检查 `depth+1`，超深在加入目录集合/待扫描栈前立即停止并关闭迭代器，文件/目录超限同样逐条立即停止，不先完整物化目录或消费其余兄弟条目。拒绝未知顶层/嵌套/非 JSON 文件、未知空目录、链接与非普通文件。源发现 `_walk_xml` 仍使用 `os.walk` 逐目录列表，是本轮已披露残差，不在该流式保证内。派生 XML 解析前拒绝 DTD/实体/其他声明/注释/CDATA/PI；manifest 的 case_id 与代表 fault 必须唯一；验证器重算状态、输出、截断与可复核安全项，不把回执布尔当作独立测量。`log_missing`/`no_failure_evidence` 是软观察原因，`ok` 允许；有输出但无 XML 覆盖、预算/读取缺口为 `partial`。
- 安全布尔区分动态可复核项（源/输出 Git/内容扫描等）与构造性不变量（新目录、无重叠、无 DTD、stdout 脱敏等）；`valid` 只代表当前批次符合本机合同，不证明历史来源未变或真实业务正确。
- 不新增第三方依赖；Python >=3.13 标准库。

## 五、验证方式

`python -m unittest discover -s tests/private_regression -p "test_*.py"` 运行纯合成自测，不需要真实源、Git 远端、设备、网络或模型；部分 junction 用例在无法创建 junction 时跳过。实际抽取与 `--validate` 需协调端阶段放行。较早版本曾执行一次只读抽取与验证（作者声明，见 `SELF_REVIEW.md` §3），整改后的候选未重跑真实数据，旧结果不能为当前实现盖章。

## 六、已知限制

- UI 派生样本只保留结构/几何/白名单状态线索，不证明语义或控件含义。
- 统一文本标记丢失文案差异；结构去重可能合并仅文案不同的快照。
- 日志白名单覆盖有限，未识别事件如实计为 `unknown`，失败窗口覆盖可能为 0；`ok` 只表示没有已知覆盖缺口，不表示所有真实失败都已观察。
- 相对时间只在窗口内有效；缺失/乱序/截断只标记，不修复。
- 日志被预算截断时只做大小/时间完整性校验，并标记 `log_integrity_scope=prefix`；发现期 stat 不能消除检查与 `open` 之间的真实并发竞态。
- XML 解析前预算使用标签无关词法粗扫，支持 Unicode 元素名但不是完整 XML Name 校验或安全解析器；正常 Unicode 属性值仍按白名单合成。
- 验证器对批次目录采用流式枚举并在上限立即停止/关闭；源发现 `_walk_xml` 仍可能按 `os.walk` 逐目录物化，属于已披露未改残差。验证器对 XML 的自由文本通道采用解析前词法拒绝，对目录/文件总量有界，但不等于完整 XML 安全审计或通用平台；真实 ACL/重解析点矩阵与并发竞态未测。
- Git 查询只区分正常 `0/1` 与操作失败，不修复损坏索引、不替代 Git、不推断失败时输出是否实际被跟踪；结束失败后已写文件保留但不回滚。
- 旧 R3 真实只读结论属较早版本作者声明，新实现未复验；`valid` 与合成测试绿灯都不构成独立审核、真实数据覆盖或业务 PASS。
- 不验证 SQLite 持久化、跨会话检索、设备执行或真实业务正确性。

## 七、同步维护要求

修改文件名、入口、回执字段、固定类别、预算、输出布局、命令或测试覆盖时，同步更新本指南、`README.md`、`SELF_REVIEW.md` 与受影响的根/父目录索引（共享索引由协调端串行维护）。新增/删除/移动文件时同一变更更新本索引。全局规则只在根指南维护。
