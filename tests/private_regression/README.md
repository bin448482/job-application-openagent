# 私有回归补充工具（旧 XML / 异常页面 / 失败时序）

用途：在**只读**前提下，从旧项目受控目录抽取少量 UI XML 与失败日志，生成纯合成派生样本和一份安全回执，作为当前离线场景测试的私有补充。它不恢复旧业务实现、不替代合成黄金集、不证明业务 PASS，也不产生任何设备动作。

所有真实输入、派生正文和 `provenance.local.json` 只保存在主项目被 Git 忽略的 `data/private-regression/<batch-id>/` 下；公开仓库只提交本工具、合成测试和说明。

当前固定候选工具版本为 `0.2.0`。回执与验证器绑定该版本；较早 `0.1.0` 的 R3 回执不属于本候选，也不能为本次整改盖章。

## 非目标

- 不执行旧项目代码，不导入其虚拟环境，不读取数据库、契约、配置、凭据或私人事实。
- 不运行 ADB、不连接设备、不调用网络/外部模型、不读 PNG、不 OCR、不使用视觉工具。
- 不把日志结果当作答案；所有派生样本的 `expected` 固定为 `needs_label`。
- 不是通用可配置 ETL。

## 使用

在项目根执行，路径用占位符替换；程序不会自动寻找根 `config.json` 或任何私人配置。

```powershell
# 合成自测（不读真实源）
python -m unittest discover -s tests/private_regression -p "test_*.py"

# 实际只读抽取：输出根必须已被 Git 忽略且未跟踪
python tests/private_regression/extract.py --source "<source-root>" --output "<main-repo>/data/private-regression"

# 本机验证器：核对字段/值白名单、节点关系、计数、安全条件与 Git 边界
python tests/private_regression/extract.py --validate "<main-repo>/data/private-regression/<batch-id>"
```

退出码：`0` 表示抽取/校验完成且安全条件全部为真；`2` 表示固定错误（参数、源缺失、路径/链接/重叠/覆盖、输出未忽略、Git 不可用、源在抽取期间被改动、预算/内部错误）。`1` 只可能来自未捕获的进程级异常，正常入口不会产生。

stdout 只输出固定 schema 的安全回执或校验结果；不输出源文件名/路径、原始哈希、真实日期、可关联源 ID、派生正文或 traceback。实际派生正文与 provenance 只写入批次目录，不向 stdout 显示。

## 受控发现

只从源项目的 `screenshots/` 和 `data/` 发现 `*.xml`，并精确识别 `data/operations.log`。不递归旧仓库其他目录，不读取其他日志。

发现/读取预算（命中即在回执标记 `truncated`/`partial`）：

| 项目 | 上限 |
|---|---:|
| 扫描文件数 | 20,000 |
| 目录深度 | 10 |
| XML 单文件 | 2 MiB |
| XML 节点 / 深度 | 6,000 / 64 |
| XML 读取总量 | 64 MiB |
| 日志单行 | 64 KiB |
| 日志行数 | 500,000 |
| 日志读取总量 | 128 MiB |
| XML 样本数 | 48 |
| 失败窗口数 | 12 |

单文件超限、深度超限或节点超限都按预算命中处理：对应故障计数保持真实，同时置 `xml.truncated=true`、总 `truncated=true`，因此 `status=partial` 而不是 `ok`。发现阶段已知超限不会误报为源改变；读取期间大小/mtime 变化则按源改变失败。

发现会跳过符号链接、junction 和其他 reparse point；源根/祖先、输出根/祖先、候选路径的原始词法组件都会在 `realpath` 之前检查。读取前还会拒绝源/输出重叠、路径逃逸和输出覆盖。日志流式读取，超长行排空也计入预算；日志打开前会再次复核 `data/` 边界与链接状态，避免发现与读取之间的替换。校验重读使用独立的有界预算。XML 解析前先按任意元素名粗扫数量/嵌套深度（不只 `<node>`），超限直接生成合成代表，未知标签也不能绕过解析前预算；未知标签在正常范围内仍按既有白名单清洗为 `node`。

本机验证器只读明确批次合同文件：先有界枚举批次内全部文件并拒绝 junction/symlink、非普通文件、未知顶层/嵌套文件和非 JSON 窗口；文件总数 ≤256（全批次总量，不是每类各 256），单 JSON ≤1 MiB，单 XML ≤2 MiB，总读取 ≤64 MiB；任何超限、未知字段或越界都返回 `invalid`。派生 XML 在解析前拒绝 DTD/实体/其他声明、注释、CDATA 与处理指令；manifest 的 sanitized `case_id` 与代表 `fault` 必须唯一并与实际文件一一对应；验证器还会重算状态与输出/截断/安全语义的自洽性，而不只相信回执布尔。

## 数据流与输出

```text
<source-root>/screenshots/**, <source-root>/data/**.xml
        │  受控发现、路径/预算检查
        ▼
白名单重构 ── 成功 ──> xml/XML-####.xml（合成树）
        └── 异常 ──> xml/representatives/<fault>.xml（纯合成代表）
<source-root>/data/operations.log
        │  流式白名单映射
        ▼
failure_windows/FW-####.json（相对时间 + 固定类别，expected=needs_label）
        ▼
manifest.json + safe_receipt.json + provenance.local.json（仅本机）
```

批次目录内容：

- `safe_receipt.json`：固定 schema 的计数/类别/安全布尔/工具版本；不含源路径、原始哈希、真实日期或自由错误文本。
- `manifest.json`：合成 case ID、kind、`expected=needs_label`、节点数/深度、属性故障计数；不含源路径。
- `xml/XML-####.xml`：白名单重构后的合成 UI 树，保留拓扑/次序、受限 Android 类、合法 bounds、布尔状态；文本统一为 `TEXT`/`DESC`，包名固定，resource-id 为随机合成别名（同值同别名）。
- `xml/representatives/<fault>.xml`：异常类别的纯合成代表，根节点带 `derived_synthetic_representative="true"` 与固定 `fault`；不复制原始片段。
- `failure_windows/FW-####.json`：固定事件/原因/状态/动作类别、重映射 `case_seq`、相对毫秒、缺失时钟/乱序/截断标志、有界数值；`expected=needs_label`。大失败簇会有界分片，尾部新类别保留在独立窗口；被裁切窗口标 `truncated=true`。
- `provenance.local.json`：仅本机的来源映射，`not_for_model=true`；实际保存绝对 `source_root`（仅该本机忽略文件，不进入 stdout/回执/manifest）、各选中样本的相对路径、大小、SHA-256 与日志完整性范围，供本机复核，不提供给模型或协调端 stdout。公开文档不记录任何实际本机路径。

固定 XML 故障类别：`dtd_or_entity`、`unsafe_markup`（注释/处理指令/CDATA/其他声明）、`invalid_encoding`、`malformed_xml`、`oversize`、`too_deep`、`too_many_nodes`、`io_error`。

固定属性故障类别：`unknown_attr`、`invalid_bounds`、`invalid_boolean`、`unknown_class`、`unknown_tag`。

固定失败类别：`transport_failure`、`executor_error`、`read_incomplete`、`filter_unverified`、`direction_unknown`、`verification_insufficient`、`cleanup_unverified`、`unread_unknown`、`action_before_failure`、`pending_crash`、`post_click_disconnect`、`local_commit_failed`；未知事件/原因/状态/动作统一映射为 `unknown`/`unattributed`，不把任意源字符串当类别带出。

失败窗口按类型证据聚类，优先保留能覆盖新失败类别的结构；大簇有界分片以保留尾部新类别。无法识别时在回执写固定 `uncovered_reason`（`no_xml_candidates`、`log_missing`、`no_failure_evidence`、`truncated_before_evidence`、`log_read_error`）。`covered_actions`/`missing_actions` 只按窗口实际出现的动作统计，`window_truncated` 标记窗口上下文被裁切，`missing_window_categories` 报告已观察但未进入任何窗口的失败类别，不发明观察结果。

本机验证器对回执、manifest、窗口与派生 XML 采用固定字段/类型/值域白名单，拒绝未知自由字段；XML 元素正文与 tail 只允许空白；读取前拒绝 junction/symlink 与越界，只读明确批次文件并受文件数/单文件/总读取预算限制。

## 状态、截断与安全布尔语义

- `ok`：有派生输出，且 XML/日志/窗口/预算均无截断或未覆盖原因。
- `partial`：有派生输出，但至少有一个预算/覆盖原因（XML 单文件/深度/节点/样本上限、日志或窗口截断、未覆盖原因等）。
- `no_candidates`：没有任何派生输出，也没有源改变/输出边界错误。
- `error`：源在抽取期间改变、日志/候选读取边界失败或输出 Git 条件失败；退出码为 `2`。源改变固定优先于 `partial`。

验证器会重算状态与输出/截断/安全语义的自洽性：`error` 必须至少有一个动态安全布尔为 false，`ok` 必须有输出且没有截断原因，`partial` 必须有输出且存在截断/未覆盖原因，不能只靠回执自述。

安全布尔分两类，不能一律当作独立测量：

- 动态/可复核：`source_unchanged` 对已读 XML 与完整日志做结束哈希复核，并对全部已发现候选做结束大小/mtime 复核；发现阶段超限或超过样本上限而未读取的候选只在该 stat 范围内复核，未做内容哈希，不能解读为“未读候选正文已逐字节证明未变”；读取失败/边界失败直接按源改变 `error`。`output_git_ignored`、`output_not_tracked` 由 Git 查询；验证器还会重算 XML 内容、禁字段与绝对日期。
- 构造性不变量：`output_new_dir`、`no_overlap`、`no_symlink_read`、`no_dtd_entity`、`stdout_sanitized`、`no_source_paths_in_receipt` 由代码路径和前置拒绝保证；验证器能复核产物内容，但不能还原历史 stdout 或证明过去某次运行从未违规。

因此本机 `valid` 只表示当前批次产物符合本机合同，不证明历史来源未变、真实抽取覆盖或业务正确性。

## 已知限制

- UI 派生样本只保留结构/几何/白名单状态线索，不证明语义、页面身份或真实控件含义；统一文本标记会丢失文案差异，结构去重可能合并仅文案不同的快照。
- 异常代表只复现故障类别，不声称原样复现，也不含原始片段。
- 日志类别映射是固定白名单；未识别的源事件如实计为 `unknown`，窗口覆盖可能为 0。旧日志不是 oracle，`needs_label` 必须另行独立标注后才可能计 PASS。
- 相对时间只在同一窗口内有效；缺时钟、乱序与截断只标记，不修复为真实序列。
- 源不变校验对完整日志做流式哈希；日志被预算截断时只做大小/时间校验并标记 `log_integrity_scope=prefix`。
- 本工具不验证 SQLite 持久化、跨会话检索、设备执行或真实业务正确性。

历史记录（作者声明，未独立验证）：早前一次经协调端放行的实际只读运行结果为 `partial`：XML 只做有界代表抽样、部分结构重复被丢弃，日志窗口全部为 `needs_label`，动作覆盖为空；具体精确聚合数只保存在本机忽略目录的安全回执中，不进入公开文档。详见 `SELF_REVIEW.md` §3。该运行对应较早的实现版本，本次整改后的固定候选**没有重跑真实数据**，也未独立复核旧回执；不能用它为当前实现或真实回归业务盖章。该结果本身也不证明全量 XML/日志覆盖或业务 PASS。
