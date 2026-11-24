# 私有回归补充工具 三轮自审记录

> 用途：记录本工具数据边界、故障输入与真实执行三轮作者自审的发现、修订和复验证据。三轮由同一作者从不同视角检查，不是独立测试人员、协调端或实机验收；R3 在协调端放行真实只读抽取后补完。

## 0 审查对象与方法

对象为 `tests/private_regression/` 下的 `extract.py`、`test_private_regression.py`、`README.md` 与双指南。R1/R2 只使用临时目录中的合成 XML/JSONL；不读取旧项目真实样本、不执行旧代码、不运行 ADB/网络/模型。每轮先列问题，再改代码/测试，最后用实际测试命令复核。

本轮前置验证（合成复现，非真实源读取）：

- Windows junction 可创建；`os.path.islink` 对其返回 `False`，但 `lstat().st_file_attributes` 含 reparse 标志，工具据此在遍历/读取前拒绝。
- Git 对尚未创建的输出路径执行 `check-ignore` 可正确判定 `data/**` 忽略；`ls-files --error-unmatch` 可判定未跟踪。
- 首次冒烟发现输出根不存在时不能直接创建批次，已改为先验证忽略、再创建输出根、最后以 `exist_ok=False` 新建随机批次。

## 1 第一轮：数据边界与脱敏

审查视角：真实/合成数据边界、XML 白名单、路径与输出隔离、stdout/回执泄露。

| 问题 | 实际发现 | 修订落点 | 复验证据 |
|---|---|---|---|
| R1-01 | `_clean_node` 构造节点后未递归 append 子节点，节点计数与拓扑都丢失；深层/超深/超多节点限制未覆盖子树 | 改为递归处理每个子节点并共用计数器/深度 | `test_topology_recurses_and_drops_unknown_fields`、`test_too_deep_and_too_many_nodes` |
| R1-02 | 早期 `_assert_within` 先 `realpath` 再检查组件，可能把原始根/祖先 junction 消掉后自洽放行 | 新增 `_assert_no_reparse_ancestors`，在 `abspath`/`realpath` 之前检查源根/祖先、输出根/祖先与候选的每个已存在组件 | `test_junction_ancestor_rejected_before_read`、`test_junction_output_ancestor_rejected`、`test_junction_child_skipped_without_read` |
| R1-03 | 单文件白名单虽已实现，但缺证据说明未知属性/真实文本/包名/资源 ID 都被替换 | 固化属性白名单、`TEXT`/`DESC`、`com.synthetic.app`、随机 `res_<8hex>`；同源 resource-id 同别名 | `test_topology_recurses_and_drops_unknown_fields`、`test_resource_alias_preserves_duplicates`、`test_unknown_class_and_tag_are_synthetic` |
| R1-04 | 只处理了常规 XML，缺少 DTD/实体/非法编码/超限/异常的独立反例 | 解析前拒绝 DTD/实体/ATTLIST/ELEMENT；BOM/严格 UTF-8；异常只生成同类别纯合成代表 | `test_dtd_and_entity_rejected`、`test_invalid_encoding_rejected`、`test_malformed_xml_rejected`、`test_oversize_xml_becomes_representative` |
| R1-05 | stdout/回执可能带源路径、合成秘密、真实日期或 traceback | 回执只含计数/固定类别/安全布尔/工具版本；顶层异常只输出固定 `error_reason`；派生正文与 provenance 不打印 | `test_stdout_and_outputs_do_not_leak_synthetic_secrets`、`test_error_paths_do_not_leak`、`test_receipt_has_no_forbidden_keys` |
| R1-06 | 早期测试把“统一文本标记”与“结构去重”混在一起，12 份只差文案的 XML 被去重成 1 份，抽样上限测试失真 | 测试改为用合法 bounds 差异构造不同结构；公开说明明确统一文本会丢失文案差异、去重可能合并仅文案不同的快照 | `test_sampling_cap_and_partial_status`；README/AGENTS 已知限制 |

复核结论：白名单重构、异常代表、路径/输出隔离与泄露反例都有独立合成输入；所有断言使用已知合成秘密串或独立预期，不用实现输出自证。未读取真实源。

## 2 第二轮：故障输入、抽样限制与独立 expected

审查视角：日志预算、截断/缺时钟/乱序、失败窗口抽样、旧 oracle、独立标注与验证器。

| 问题 | 实际发现 | 修订落点 | 复验证据 |
|---|---|---|---|
| R2-01 | 日志被预算截断时 `ReadRecord.sha256` 只是前缀，校验却读完整文件比较，导致未变化源被判 False；校验还用无界 `read_bytes()` | 区分完整/前缀完整性；完整日志流式哈希，前缀只校验大小/时间并标记 `log_integrity_scope=prefix`；校验重读使用独立预算 | `test_read_budget_is_bounded`、`test_source_change_sets_error_and_nonzero_exit` |
| R2-02 | 超长日志行排空没有累计全局字节/行预算，可绕过 `MAX_LOG_BYTES` | 超长行排空也逐块扣减预算；预算耗尽即 `truncated` | `test_read_budget_is_bounded` |
| R2-03 | 单一批次共用 XML/日志读取预算，XML 可能耗尽预算导致日志被无谓截断 | 拆为 XML 读取、日志读取、XML 校验、日志校验四个独立预算；回执分别报告用量/上限/耗尽 | `test_read_budget_is_bounded`；验证器 `budget_consistent` |
| R2-04 | 源在抽取期间变化时早期只返回安全布尔，仍可能 status=ok/exit 0 | 安全条件失败改为 `status=error`、固定 `error_reason=source_changed`、退出码 2；验证器拒绝 `safety_all_true=false` | `test_source_change_sets_error_and_nonzero_exit`、`test_validator_rejects_safety_false_and_bad_xml` |
| R2-05 | 失败窗口可能把未知事件当证据，或把乱序时间静默排序 | 只有固定失败类别/原因/失败状态才进入窗口；未知一律 `unknown`；缺时钟/乱序只标记不修复 | `test_only_unknown_events_reports_uncovered`、`test_clock_missing_marked`、`test_out_of_order_not_silently_fixed` |
| R2-06 | 旧日志中的 `result/expected/status=PASS` 可能被误当 oracle 或状态 | 这些值不在状态白名单，映射为 `unknown`；窗口与 manifest 的 `expected` 固定 `needs_label` | `test_old_oracle_fields_not_trusted`、`test_manifest_and_windows_all_needs_label` |
| R2-07 | 早期验证器只核对 schema 名与部分键，且引用未定义 `windows` 变量会 `NameError` | 重写验证器：实际读取窗口、校验固定字段/值白名单、XML 属性/文本/类/节点关系、case ID 与实际文件一一对应、manifest 形状、计数、safety、budget、Git 边界、禁字段与绝对日期 | `test_validator_accepts_valid_batch`、`test_validator_rejects_tampered_window`、`test_validator_rejects_safety_false_and_bad_xml`、`test_validator_rejects_forbidden_receipt_key` |
| R2-08 | provenance 早期用 samples 与全部 read_records zip，异常/重复读取会造成第二个样本指向错误源 | 选中样本时直接记录 `case_id → ReadRecord`；异常/重复读取不进入 provenance | `test_provenance_maps_selected_cases_not_read_order` |
| R2-09 | 无候选、仅未知事件、超限/深度/节点异常可能被静默当成成功覆盖 | 回执明确 `no_candidates`/`partial`/`uncovered_reason`/`missing_actions`；异常进入代表并计数 | `test_no_candidates_reports_missing`、`test_only_unknown_events_reports_uncovered`、`test_oversize_xml_becomes_representative` |
| R2-10 | 窗口去重签名只含事件/原因/状态/动作，结构相同但乱序的第二个窗口被吞掉，乱序证据丢失 | 签名加入 `clock_missing`、`out_of_order` 与相对毫秒序列；同结构不同时序不再合并 | `test_window_dedup_distinguishes_time_order` |
| R2-11 | 连续大失败簇按固定 40 事件从头部截断，尾部新类别可能不在任何窗口，且窗口仍标 `truncated=false` | 大簇按 `MAX_WINDOW_EVENTS - 2*WINDOW_PAD` 有界分片，尾部独立成窗；裁切窗口标 `truncated`，回执新增 `window_truncated` 与 `missing_window_categories` | `test_long_cluster_keeps_tail_category` |
| R2-12 | 超长物理行未计入行数上限，可借无效长行绕过 `MAX_LOG_LINES`；NaN/Infinity 数值可使整批中断 | 每个物理行先计数再判上限；`_bounded_numbers` 拒绝非有限浮点 | `test_long_physical_line_counts_toward_line_cap`、`test_nonfinite_numbers_ignored` |
| R2-13 | 验证器只查少数禁用键，未拒绝 XML 字符正文/tail 或任意自由字段；读取未做批次路径 reparse/预算限制 | 验证器改为固定字段/类型/值域白名单，XML 正文与 tail 只允许空白，读取前拒绝 junction/symlink 并用独立预算/文件数上限 | `test_validator_rejects_xml_character_data`、`test_validator_rejects_arbitrary_free_fields`、`test_validator_rejects_junction_batch` |

复核结论：预算、截断、缺时钟、乱序、旧 oracle、抽样与验证器都有独立合成反例；`needs_label` 不被任何日志字段覆盖。未读取真实源。

## 3 第三轮：真实执行、隔离、报告与待提交内容

状态：记录的是**较早实现版本**下一次经协调端阶段审查放行的只读抽取，属作者声明、未由独立审核端验证。整改后的固定候选没有重跑真实数据，旧回执不能为当前实现盖章；本轮只做合成输入与静态范围复核。公开文档不记录真实执行日期、源路径、源文件名、哈希、唯一批次 ID、真实样本与扫描规模、业务事件计数或类别分布；以下只用固定占位符与定性结论，精确真实统计仅保留在本机 Git 忽略目录的安全回执中。

实际命令（项目根）：

```powershell
python tests/private_regression/extract.py --source "<source-root>" --output "<main-repo>/data/private-regression"
python tests/private_regression/extract.py --validate "<main-repo>/data/private-regression/<batch-id>"
```

退出码：抽取 `0`，本机验证器 `0`。安全回执相对位置：`data/private-regression/<batch-id>/safe_receipt.json`。未手工显示或提交真实源正文、派生正文或 `provenance.local.json`。

### 3.1 实际安全回执摘要（已脱敏）

| 项目 | 公开结论 |
|---|---|
| 批次 | 唯一新批次，未覆盖旧批次；批次 ID 不公开 |
| `status` | `partial`（XML 样本上限命中，窗口上下文有裁切） |
| 发现 | 受控范围内发现 XML 候选与失败日志；文件/深度上限未命中，无超限、无链接读取、无非普通文件读取；精确计数仅本机 |
| XML | 只保留有界代表抽样，存在结构重复丢弃；`truncated=true`、`uncovered_reason=none`；精确样本数仅本机 |
| XML 故障 | 本次抽样未命中任何固定故障类别；这不等于源中无异常样本 |
| 日志 | 存在少量无效行被跳过，日志读取未截断，产生有界失败窗口，全部 `needs_label`；精确行数/窗口数仅本机 |
| 未知映射 | 固定白名单下事件/原因/状态与动作存在大量 `unknown`/`unattributed`；精确计数仅本机 |
| 时间标志 | 本次窗口未出现缺时钟或乱序；存在窗口裁切 |
| 失败类别 | 只有部分固定失败类别被观察到，其余类别计数为 0；精确分布仅本机 |
| 覆盖/缺失 | `covered_actions=[]`、A01–A08 全部缺失、`missing_window_categories=[]`、`uncovered_reason=none` |
| 安全布尔 | source_unchanged、output_new_dir、output_git_ignored、output_not_tracked、no_overlap、no_symlink_read、no_dtd_entity、no_absolute_dates、stdout_sanitized、no_source_paths_in_receipt 全为 true |
| 预算 | XML 与日志读/校验预算均未耗尽；日志完整性 `full`；精确字节数仅本机 |
| 本机验证器 | 当时版本 `valid`，20 项检查全为 true；整改后验证器检查项已增加，旧结果不代表新实现 |

### 3.2 第三轮发现与处理

| 检查 | 实际发现 | 处理/结论 |
|---|---|---|
| R3-01 隔离 | 输出根此前不存在；本次只生成一个随机新批次；Git 忽略检查退出 0、跟踪文件 0、主仓库该目录状态为空 | 未覆盖旧批次；未把输出加入 Git |
| R3-02 样本覆盖 | XML 候选数超过抽样上限，只选中有限份有界代表，部分结构重复被丢弃；`xml.truncated=true`，未命中任何 XML 故障类别 | 只能声明“有界代表抽样”，不能声称覆盖全部 XML 候选；无异常样本不等于源中无异常 |
| R3-03 日志覆盖 | 少量无效行被跳过；固定白名单下 unknown 事件/原因/状态与 unattributed 动作占比较高 | 白名单映射有限；未识别内容如实计为 unknown，不把自由文本带出 |
| R3-04 动作覆盖 | 失败窗口均无动作类别，`covered_actions=[]`，A01–A08 全部缺失 | 不把窗口归因到具体动作；不发明动作观察结果 |
| R3-05 待标注 | 全部失败窗口与 XML/manifest 样本 `expected=needs_label` | 旧日志结果未作 oracle；无独立标注，不计业务 PASS |
| R3-06 窗口裁切 | `window_truncated=true`，长失败簇按有界分片处理 | 已观察失败类别均进入至少一个窗口（`missing_window_categories=[]`），但窗口上下文有裁切，不能宣称完整时序 |
| R3-07 隐私与提交 | 回执/验证器 stdout 未出现源路径、文件名、原文、真实日期、哈希或派生正文；验证器 `no_forbidden_keys=true`、`no_absolute_dates=true` | 仅提交公开工具、合成测试与指南；真实/派生产物保持本机忽略目录，公开文档不含精确聚合数 |

复核结论：真实只读抽取和本机验证器均成功退出，安全条件全为 true；但结果是 `partial`，XML 只做有界代表抽样、日志映射存在大量 unknown、动作覆盖为空，因此不证明全量 XML/日志覆盖、不证明业务 PASS。真实派生正文与 provenance 未显示、未提交；精确真实统计只留在本机忽略目录的安全回执。

## 4 整改后实际校验

```powershell
python -m unittest discover -s tests/private_regression -p "test_*.py"
```

最近一次结果：87 项测试通过（上一轮 82 项判据全部保留 + 本轮 Git 故障安全新增 5 项）、0 失败、0 错误、0 跳过，退出码 0。另以独立审核端提供的合成探针在本机副本逐项复跑，B-1/B-2/B-3/N-1/N-2 的旧探针结论均已翻转；证据只写在本机 Git 忽略目录。该结果只证明合成判据与工具链路，不证明真实抽取、真实数据覆盖、独立审核通过或业务 PASS。

旧 R3 实际只读抽取与验证结果对应较早实现版本（作者声明），本轮未重跑真实数据，也未独立复核旧回执；不能用旧结果证明当前实现。公开提交只含 6 个工具/测试/指南文件，真实/派生产物仍只在被忽略目录。上述结果不代表真实模型、设备、独立标注或业务 PASS。

## 5 整改后三轮作者自审（合成范围）

> 本节由同一作者在收到独立只读审核 B-1/B-2/B-3 与 N-1～N-7 后追加，仍是作者自审，不是独立审核、协调端验收或真实回归业务 PASS。三轮都只使用临时合成目录、临时 Git 仓库与合成本机文件。

### 5.0 问题整改矩阵

| 编号 | 旧问题 | 整改落点 | 复验（合成） | 未测边界 |
|---|---|---|---|---|
| B-1 | 超限/过深/节点过多仍可 `status=ok`、`xml.truncated=false` | `xml_truncated` 汇总单文件/深度/节点/样本/扫描预算；故障计数保持真实 | 四类正例均 `partial`+`xml.truncated=true`；普通批次仍 `ok` | 真实 2 MiB/64 层/6000 节点源未重跑 |
| B-2 | `stat` 后增长/缩短/替换仍报 `source_unchanged=true` | 读取前复核大小、读取后复核大小/mtime；发现候选全部在结束复核；读取边界失败记 `source_changed` | 增长 CLI 退出 2、error、safety=false；缩短与等长替换亦 error；发现期已知超限仍 partial | 真实文件系统竞态/同步工具未测 |
| B-3 | 重复 `case_id`、XML 注释、DTD/实体仍 `valid` | manifest `case_id`/代表 fault 唯一性；验证器与抽取器解析前拒绝 DTD/实体/其他声明/注释/CDATA/PI | 重复条目、注释、DOCTYPE 均 `invalid`；抽取器新增 `unsafe_markup` 代表 | 真实旧 XML 未读取 |
| N-1 | 矛盾 status 可通过 | 验证器重算 error/ok/partial/no_candidates 与输出、截断、safety 的交叉条件 | error+全 true、ok+无输出、partial+无截断均 `invalid` | 无真实回执重放 |
| N-2 | 忽略额外文件、文件数按类别 | 有界枚举全批次，拒绝未知顶层/嵌套/非 JSON/链接；文件数总量 ≤256 | 额外嵌套/根文件/junction 均 `invalid`；总量 4 拒绝、5 通过 | 真实 Windows ACL/重解析点矩阵未测 |
| N-3 | 日志读取缺边界复核 | 打开前重核 `data/` 边界、链接与 stat；异常按源改变失败 | 发现后 junction 替换在 open 前拒绝且不打开源外文件 | 真实并发替换时序未测 |
| N-4 | 预扫只识别 `<node>` | 改为标签无关的深度/宽度预扫；未知标签仍按既有白名单清洗 | 未知标签深/宽树在 `ET.fromstring` 前触发异常 | 恶意超长属性/畸形标签仅粗扫，未做完整解析前语义验证 |
| N-5 | provenance 绝对根与文档不符 | 文档明确本机忽略文件保存绝对 `source_root`、`not_for_model=true`、不进入 stdout/回执/manifest | 合成测试核对实际行为与验证器 | 真实路径未公开、未读取 |
| N-6 | 安全布尔被当作独立测量 | 文档区分动态可复核与构造性不变量；验证器重算可复核项 | XML 内容/禁字段/日期/Git 重算；矛盾回执拒绝 | 历史 stdout 与过去运行无法事后证明 |
| N-7 | 缺对应反例 | 新增 24 项合成测试并保留原 45 项断言 | 69 项全部通过，未删除/放宽原断言 | 真实数据/设备/模型未测 |

### 5.1 第四轮：预算与源变化语义

审查视角：单文件/深度/节点预算、读取期间源变化、发现期已知超限的区分。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R4-01 | 超限候选只登记代表和计数，没有进入 `xml.truncated`，可返回 `ok` | 汇总 `xml_truncated` 并让总 `truncated` 继承 | 超限/过深/节点过多三类测试与生产阈值探针均 `partial` | 真实大文件性能未测 |
| R4-02 | `_read_xml` 把读取期间增长降级为 `oversize`，并跳过源不变校验 | 新增 `_SourceChanged`；读取前后复核大小/mtime，候选表结束复核 | 增长/缩短/等长替换均 `error`、`source_unchanged=false`；增长 CLI 退出 2 | 真实并发写入未测 |
| R4-03 | 发现候选未读时没有可核验状态 | 发现阶段记录每个候选大小/mtime，结束统一复核 | 合成测试覆盖发现期超限仍 partial 且 safety=true | 权限/ACL 变化未测 |
| R4-04 | 预留预算/正常批次可能被新语义误伤 | 只有三类 XML 预算与既有截断进入 `xml_truncated` | 边界负例仍 `ok`、`xml.truncated=false` | 真实批次规模未测 |

本轮复核：预算命中不再伪装完整成功；源变化与已知超限已分开；未读取真实数据。

### 5.2 第五轮：验证器与自由文本通道

审查视角：manifest 唯一性、XML 声明/注释/处理指令、状态矛盾、批次文件边界。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R5-01 | 重复 `case_id` 可折叠集合并同时抬高 selected | schema 与 entries 两层拒绝重复 case_id 与代表 fault | 重复 manifest 与重复代表均 `invalid` | 无真实 manifest |
| R5-02 | 默认 ElementTree 丢弃注释、展开实体，验证器仍 `valid` | 抽取/校验共用解析前拒绝：DTD/实体/其他声明/注释/CDATA/PI | 注释、DOCTYPE、PI 反例均拒绝；抽取器输出 `unsafe_markup` 代表 | 只做词法拒绝，未做完整 XML 安全研究 |
| R5-03 | `status=error` 但 safety 全 true 可 `valid`；无输出可 `ok` | 重算 status 与输出/截断/safety 交叉条件 | 三个矛盾回执均 `invalid` | 无真实错误回执 |
| R5-04 | 额外嵌套/非 JSON 文件被忽略；文件数按类别 | 有界枚举全批次并与声明集合逐项比对；总量上限 | 额外目录、根文件、junction、总量边界均按预期 | 真实 ACL 未测 |
| R5-05 | provenance 仅检查存在 | 校验 schema、`not_for_model`、batch、样本映射与日志字段 | 正常批次 `provenance_ok=true`；篡改由合成测试覆盖 | 不校验真实源内容 |

本轮复核：`valid` 不再由单条安全布尔或集合折叠产生；验证器仍只读受控批次文件。

### 5.3 第六轮：竞态、未知标签、provenance 与文档边界

审查视角：日志 TOCTOU、未知标签预扫、provenance 实际行为、安全布尔语义、旧 R3 声明与测试完整性。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R6-01 | 日志读取未复核边界/链接 | 打开前重核根、链接与 stat；失败按源改变处理 | junction 替换在 open 前拒绝，未读取源外文件 | 真实竞态窗口未测 |
| R6-02 | 预扫只数 `<node>`，未知标签先入解析器 | 标签无关粗扫，保留未知标签清洗语义 | 深/宽未知标签在解析前触发预算异常 | 复杂属性/畸形文本仅粗扫 |
| R6-03 | provenance 绝对根未说明 | 公开说明只描述行为，不写实际值；本地验证器复核 `not_for_model` 与映射 | 合成测试核对绝对根与 stdout/manifest 隔离 | 真实路径未公开 |
| R6-04 | 安全布尔混合了构造与测量 | 文档分列动态/构造项；验证器重算可复核项 | 文档与验证器测试同步 | 历史 stdout 无法事后证明 |
| R6-05 | 旧 R3 结果可能被误用为新实现证据 | README/SELF_REVIEW 明确旧版本作者声明、未重跑、不可盖章 | 文档静态复核 | 真实只读抽取未重跑 |
| R6-06 | 原测试绿灯未覆盖独立探针 | 保留原 45 项，新增 24 项反例；用独立探针副本逐项对照 | 69 项通过；11 个探针输出全部翻转 | 不是独立审核通过 |

本轮复核：补充的是合成反例与文档一致性，不把无法复核的历史来源不变说成新验证器已测，也不声称真实回归业务 PASS。

### 5.4 上一轮三轮自审总结

- 当时实际命令：`python -m unittest discover -s tests/private_regression -p "test_*.py"`；结果 69 项通过、0 失败、0 错误、0 跳过、退出 0。
- 独立审核探针本机副本：B-1/B-2/B-3/N-1/N-2 共 11 项旧结论全部翻转；证据只写在本机忽略目录。
- 仍未验证：真实旧 XML/日志/provenance、真实 R3 重跑、真实 Windows ACL/junction 矩阵、设备/ADB、网络/模型、独立标注与业务 PASS。
- 结论仅为作者在合成范围内完成整改并通过本机判据，不代表独立审核通过，也不代表可跳过原审核端复审。

## 6 abb63de 复审返修后三轮作者自审（合成范围）

> 本节针对独立复审 `abb63de` 的 changes_requested 追加。仍是同一作者自审，不是独立审核、协调端验收或真实回归业务 PASS；只使用临时合成目录、临时 Git 仓库与合成本机文件，未读取真实旧 XML/日志/provenance 或旧 R3 回执。

### 6.0 复审问题矩阵

| 编号 | 复审问题 | 整改落点 | 复验（合成） | 未测边界 |
|---|---|---|---|---|
| 阻塞 log-only | 只有日志失败窗口时抽取 `ok`/退出 0，验证器却 `invalid`/退出 2 | 生成器把“有输出但 `xml.uncovered_reason=no_xml_candidates`”计入覆盖缺口，统一走 `partial`；验证器沿用同一原因定义 | 抽取 `partial`、窗口保留、XML 缺口如实、验证 `valid`，两个 CLI 入口都退出 0 | 真实日志/XML 组合未重跑 |
| N-4 Unicode | 预扫只识别 ASCII 元素名，Unicode 深/宽树仍先进入 `ET.fromstring` | 标签无关词法粗扫接受任意受支持元素名；保留未知标签清洗与 Unicode 属性值规范化 | Unicode 深/宽在解析前触发预算，`ET.fromstring` 0 次调用；普通 Unicode 属性值正常合成 | 不是完整 XML Name/安全解析器 |
| N-1 软原因 | `log_missing`/`no_failure_evidence` 与 `ok` 的关系文档不一致 | 明确软观察原因允许 `ok`；XML 缺口、预算/日志读取缺口才强制 `partial`；生成/验证共用原因集合 | XML-only、XML+无失败日志、log-only、空源、预算截断组合逐项校验 | `ok` 仍不证明真实失败覆盖完整 |
| B-2 日志变化 | 发现后到读取前的日志变化未纳入源不变约束 | 发现阶段记录日志 stat；pre-open、读取后、结束复核都核对；变化按 `source_changed`/退出 2 | 增长/缩短/替换均 error、safety=false；pre-open 不一致时不读取 | 真实并发 `check/open` 竞态未测 |
| N-2 目录 | 空目录不计入布局，目录宽度无上限 | 有界枚举目录与文件；未知空目录拒绝；子目录总量 ≤16、文件 ≤256、深度 ≤4 | 未知空目录 invalid；合法 3 目录通过、超限或额外目录拒绝 | 真实 ACL/重解析点矩阵未测 |
| N-7 覆盖 | 缺 log-only、Unicode、日志变化等回归 | 保留原 69 项，新增 9 项组合/边界测试 | 78 项通过；独立探针 before/after 对照 | 非独立审核通过 |

### 6.1 第七轮：状态组合与独立 oracle

审查视角：抽取/验证状态是否互斥，`ok`/`partial`/`no_candidates`/`error` 与软原因、覆盖缺口是否一致。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R7-01 | log-only 窗口被抽取成 `ok`，验证器却按 XML 缺口拒绝 | 把有输出且无 XML 覆盖计入 `partial`，不松验证器 | 抽取 `partial`、`validate` valid、两个 CLI 退出 0；窗口仍为 `needs_label` | 真实组合未重跑 |
| R7-02 | `log_missing`/`no_failure_evidence` 的 `ok` 语义未写清 | 定义为软观察原因，允许 `ok` 但不得暗示全覆盖 | XML-only 与无失败日志组合 `ok`+valid；空源/截断组合单独断言 | 独立标注与业务覆盖未测 |
| R7-03 | 生成器与验证器各自使用字面原因集合，易再次漂移 | 提取 `XML_UNCOVERED_*`/`SOFT_LOG_UNCOVERED`/`COVERAGE_LOG_UNCOVERED` 共用定义 | 16 种源组合矩阵 status 与 validator 全部一致 | 原因集合仍是本机合同 |
| R7-04 | 审查者输出可能被当 oracle | 先用独立探针复现 before，再仅按公开契约与合成输入判断 after | before 五项失败、after 全部翻转；作者输出未作为预期来源 | 不构成独立审核 |

本轮复核：状态规则由同一原因集合驱动；没有通过放宽验证器把覆盖缺口伪装成 `ok`。

### 6.2 第八轮：解析前预算与源时序

审查视角：Unicode 元素名、未知标签、日志发现期 stat、pre-open 与结束复核。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R8-01 | Unicode 名称不匹配 ASCII 正则，深/宽树先完整解析 | 词法粗扫改为任意元素名，保留预算与清洗语义 | Unicode 深/宽解析调用 0；ASCII 既有行为不变 | 非完整 XML Name 校验 |
| R8-02 | 日志发现后到读取前变化仍可能 `source_unchanged=true` | 记录发现期日志 stat，pre-open/读取后/结束三段复核 | 增长/缩短/替换 error+source_changed+退出 2；pre-open 变化禁止读取 | 真实竞态窗口未测 |
| R8-03 | 同大小同 mtime 替换无法仅靠 stat 发现 | 文档明确 stat/已读哈希承诺边界，不夸大原子保证 | README/AGENTS 与探针边界一致 | 未做真实并发矩阵 |
| R8-04 | 解析前防护可能被误写成完整解析器 | 文档声明只是预算粗扫，不是安全解析器 | 普通 Unicode 属性值仍正常合成 | 完整 XML 安全审计未做 |

本轮复核：Unicode 与日志时序问题均以“解析/读取前”可观察行为判定，不依赖解析后清洗或最终哈希掩盖。

### 6.3 第九轮：最终归档、自包含与范围

审查视角：最终固定 Git archive 是否自包含、测试/探针是否依赖未跟踪文件、范围与旧敏感提交边界。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R9-01 | 工作区绿灯不能证明归档自包含 | 从最终 `git archive` 解包运行同一 unittest 与关键探针 | 归档 78 项通过、探针 after 一致、无未跟踪依赖 | 真实源/设备未测 |
| R9-02 | 新增测试可能无意删除旧判据 | 检查测试文件相对 `abb63de` 只有新增 | 原 69 项全部保留，新增 9 项；78 项 0 失败/错误/跳过 | 测试通过不等于业务 PASS |
| R9-03 | 目录枚举可能以空目录/宽度绕过 | 目录与文件分别总量限制，固定合法布局 | 空目录、额外目录、总量边界均有反例 | 真实 ACL 未测 |
| R9-04 | 旧 R3/旧固定包结果可能被继承 | 版本升为 `0.3.0`，旧回执不属本候选 | 文档声明版本、作者、未独立验证 | 真实 R3 未重跑 |

本轮复核：最终只提交六文件范围内的必要源码/测试/指南；未改 app、共享指南、docs、`.gitignore` 或真实数据。

### 6.4 本轮三轮自审总结

- 实际命令：`python -m unittest discover -s tests/private_regression -p "test_*.py"`；78 项通过、0 失败、0 错误、0 跳过、退出 0；最终归档重跑同结果。
- 独立复审关键探针：log-only 抽验双入口、Unicode 解析调用、日志发现后变化、状态矩阵均从 before 失败翻转为 after 通过；证据只在本机忽略目录。
- 仍未验证：真实旧 XML/日志/provenance、真实 R3 重跑、真实 Windows ACL/并发替换矩阵、设备/ADB、网络/模型、独立标注与业务 PASS。
- 结论仅为作者在合成范围内完成返修并通过本机判据，必须由原独立审核端复审新固定 SHA，不能以 78 项绿灯或本节自审代替。

## 7 f211f83 N2 流式枚举返修后三轮作者自审（合成范围）

> 本节针对独立复审 `f211f83` 的 changes_requested 追加。仍是同一作者自审，不是独立审核、协调端验收或真实回归业务 PASS；只使用临时合成目录、临时 Git 仓库与合成本机文件，未读取真实旧 XML/日志/provenance 或旧 R3 回执。

### 7.0 复审问题矩阵

| 编号 | 复审问题 | 整改落点 | 复验（合成） | 未测边界 |
|---|---|---|---|---|
| 阻塞 N2 | `list(os.scandir(current))` 先物化单目录全部条目，再检查文件/目录上限 | 改为 `contextlib.closing(os.scandir(...))` 流式迭代；每个条目计数后立即检查上限并 `raise`，由上下文关闭迭代器 | 单目录 300 额外文件从 before `305` 条降为 after `257` 条；目录超限在 5 个目录处停止；全部探针 `closed=true` | 真实 ACL/并发替换未测 |
| 深度边界 | 缺作者回归证明深度 5 不先遍历更深内容 | 保持深度 >4 在 `scandir` 前拒绝 | 深度 4 目录自身被扫描；深度 5 目录未被 `scandir` | 深层真实文件系统未测 |
| 合法批次 | 流式改动可能误伤正常批次/原 78 项 | 保留未知目录/空目录/链接/总量与所有旧断言 | 原 78 项全部保留，新增 3 项后 81 项通过；合法批次仍 valid | 真实批次未跑 |
| 文档边界 | 容易把批次验证器流式说成源发现也已证明 | README/AGENTS 明确流式仅限批次验证器；`_walk_xml` `os.walk` 是已披露残差 | 文档静态复核 | 源发现资源行为未改 |

### 7.1 第十轮：流式实际消耗、close、阈值与独立预期

审查视角：是否在上限检查前物化目录；超限是否立即停止并关闭；测试是否看实际消费而不是只看最终 `invalid`。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R10-01 | `list(os.scandir)` 在 256 文件上限前消费 305 条 | 改为流式迭代，逐条即时检查 | 独立同一探针 after `entries_yielded=257`，低于 before 305 且小于全目录条目 | 未做真实超大目录性能验收 |
| R10-02 | 目录上限同样在完整物化后统计 | 目录加入集合后立即检查并 raise | 100 个额外空目录下仅消费 ≤8 条并关闭 | 未做真实宽度文件系统矩阵 |
| R10-03 | 深度检查在弹出目录时，缺作者回归 | 保持超深度在 `scandir` 前拒绝并补测试 | 深度 4 自身被扫描、深度 5 未被扫描 | 真实深层 ACL 未测 |
| R10-04 | 只看最终 `invalid` 不能证明提前停止 | 用 `_CountingScandir` 断言实际产出数与 `closed` | 生产 256 阈值、目录小阈值与深度 5 均断言实际消费/关闭 | 测试包装不替代真实 OS 并发 |

本轮复核：结论基于实际消费条目与 close 状态，而不是只看验证器最终返回。

### 7.2 第十一轮：既有状态/Unicode/日志时序/链接/合法批次保持

审查视角：本轮只改枚举是否破坏上一轮已闭合行为。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R11-01 | 原 78 项可能被流式改动破坏 | 只替换枚举实现，不改状态/Unicode/日志/链接逻辑 | 原 78 项全部通过，新增 3 项后 81 项 0 失败/错误/跳过 | 非独立批准 |
| R11-02 | log-only/软原因/状态矩阵可能回归 | 保持共用原因集合与验证器规则 | `status_matrix.py` 16 例仍全部自洽，log-only 双入口仍 0/0 | 真实组合未跑 |
| R11-03 | Unicode 解析前预算可能回归 | 不改 `TAG_RE` 与预算顺序 | Unicode 深/宽 `parsed_before_budget=false`，解析调用 0 | 非完整 XML 解析器 |
| R11-04 | 未知目录/空目录/链接/合法批次行为可能变化 | 保留目录集合与固定布局检查 | 空目录/额外目录/链接仍 invalid，合法批次 valid | 真实 ACL 未测 |

本轮复核：流式改造限定在批次枚举，未重开已闭合的状态、Unicode、日志时序问题。

### 7.3 第十二轮：最终 Git 归档、原 78 保留、范围与源码哈希

审查视角：最终固定包是否自包含、是否只改允许文件、原判据是否保留、源码是否被测试改写。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R12-01 | 工作区通过不等于归档自包含 | 从最终 `git archive` 解包运行同一 unittest 与关键探针 | 归档 81 项通过、N2 探针 after 一致、无未跟踪依赖 | 未跑真实源 |
| R12-02 | 新增测试可能删除旧判据 | 原 78 项全部保留，仅尾部新增 | 测试文件相对 `f211f83` 纯新增 3 项；81 项通过 | 非业务 PASS |
| R12-03 | 改动可能越出允许六文件 | 只改现有五文件，`CLAUDE.md` 未动 | 相对 `f211f83` 恰 5 个修改；相对 `5cb6817` 恰 6 个新增 | 未合并主树 |
| R12-04 | 测试可能改写源码 | 运行前后计算六文件 SHA256 | 前后全部一致 | 不代表真实数据 |

本轮复核：最终交付限定在六文件范围内，归档可重跑，原 78 项保留；仍需原独立审核端复审新固定 SHA。

### 7.4 本轮三轮自审总结

- 实际命令：`python -m unittest discover -s tests/private_regression -p "test_*.py"`；81 项通过、0 失败、0 错误、0 跳过、退出 0；最终归档重跑同结果。
- N2 独立探针：before 305 条、after 257 条；目录与深度测试断言实际扫描集合与 close，而非只看 `invalid`。
- 仍未验证：真实旧 XML/日志/provenance、真实 R3、真实 Windows ACL/并发替换、设备/ADB、网络/模型、独立标注与业务 PASS；源发现 `_walk_xml` 的 `os.walk` 逐目录模式仍是已披露未改残差。
- 结论仅为作者在合成范围内完成 N2 流式返修，必须由原独立审核端复审，不能以 81 项绿灯或本节自审代替。

## 8 e0de71b 深度发现即停返修后三轮作者自审（合成范围）

> 本节针对独立复审 `e0de71b` 的 changes_requested 追加。仍是同一作者自审，不是独立审核、协调端验收或真实回归业务 PASS；只使用临时合成目录、临时 Git 仓库与合成本机文件，未读取真实旧 XML/日志/provenance 或旧 R3 回执。

### 8.0 复审问题矩阵

| 编号 | 复审问题 | 整改落点 | 复验（合成） | 未测边界 |
|---|---|---|---|---|
| 阻塞 N2 深度 | 深度检查只在 `stack.pop()` 后；发现深度 5 子目录时先 `dirs.add`/`stack.append`，继续消费深度 4 当前目录其余 100 条目 | 在 `entry.is_dir` 分支、入集合/栈之前检查 `depth+1 > MAX_VALIDATE_DEPTH` 并立即 `raise`；保留 pop 防御和 `contextlib.closing` | 同一独立探针 before `yielded=112` → after `yielded=12`；`d4_scanned=false`、`closed=true` | 真实 ACL/并发未测 |
| 同级消费 | 作者旧测试只证明 `d4` 未 scan，未证明同级剩余条目提前停止 | 新增固定顺序回归：`d4` 首项 + 100 兄弟文件 | `d3` 只消费触发项 1 个，`remaining_yielded=0`，100 兄弟零消费 | 测试包装不替代真实 OS 顺序 |
| 生产/小阈值 | 需确认生产 4 允许、5 拒绝与小阈值边界 | 保留生产 4 与 pop 防御，新增小阈值控制 | 深度 4 自身扫描；深度 5 不 scan；小阈值 2 时深度 3 不 scan | 真实深目录未跑 |
| 既有 81 项 | 修改深度分支可能回归文件/目录/状态 | 只加前置深度检查，不改其他路径 | 原 81 项全部保留，新增 1 项后 82 项通过 | 非独立批准 |

### 8.1 第十三轮：深度触发与同级消费独立 oracle

审查视角：是否在发现超深子目录的当下停止；是否仍消费当前目录其余条目；是否关闭迭代器。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R13-01 | 深度 5 发现后延迟到 pop 才拒绝 | 在 `is_dir` 分支先检查 `depth+1`，超限立即 raise | 独立同一探针 before 112 → after 12 条 | 真实目录顺序未测 |
| R13-02 | `d3` 的 101 个条目全部消费 | raise 发生在当前 `with` 内，立即关闭当前 `scandir` | `d3` 只消费触发项，100 兄弟零消费；`closed=true` | 测试用固定顺序控制 |
| R13-03 | 只看 `d4` 未 scan 不足以证明时序 | 增加 `_FirstEntryScandir` 与消费计数断言 | `yielded=1`、`remaining_yielded=0`、更深目录未扫描 | 非真实文件系统基准 |
| R13-04 | 小深度阈值缺控制 | 补 patch `MAX_VALIDATE_DEPTH=2` 的链式目录 | 深度 3 未被 scan，全部 close | 真实深层 ACL 未测 |

本轮复核：结论基于实际条目消费/更深入口调用/close，而不是只看最终 `invalid`。

### 8.2 第十四轮：原 81/文件目录深度与状态边界保持

审查视角：前置深度检查是否误伤合法批次、文件/目录流式上限、状态/Unicode/日志/链接。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R14-01 | 原 81 项可能被深度分支改动破坏 | 只增加前置检查，不改文件/目录/状态逻辑 | 原 81 项全部通过，新增 1 项后 82 项 0 失败/错误/跳过 | 非独立批准 |
| R14-02 | 生产文件 256/目录 16 流式消费可能回归 | 保留文件/目录逐条检查与 close | 生产文件 `yielded=257/305`、目录 `yielded=17/105`、小阈值 5、全 closed | 真实大目录未跑 |
| R14-03 | 合法结构/深度 4 可能误拒 | 深度 4 自身仍允许扫描，只有 depth+1>4 拒绝 | 深度 4 合法链扫描、深度 5 拒绝 | 真实布局未跑 |
| R14-04 | log-only/Unicode/日志变化/链接保持 | 本轮不改这些路径 | 状态矩阵、log-only 双入口、Unicode 零解析、日志变化、链接仍通过 | 真实组合未跑 |

本轮复核：返修限定在深度发现即停，原 81 项与既有状态边界保持。

### 8.3 第十五轮：最终归档、范围、哈希与文档保证一致

审查视角：最终 Git archive 自包含、只改允许文件、原 81 保留、源码前后哈希、文档不再夸大深度流式保证。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R15-01 | 工作区通过不等于归档自包含 | 从最终 `git archive` 解包运行同一 unittest 与关键探针 | 归档 82 项通过、深度探针 after 一致、无未跟踪依赖 | 未跑真实源 |
| R15-02 | 新增测试可能删除旧判据 | 原 81 项全部保留，仅尾部新增 1 项 | 测试文件相对 `e0de71b` 纯新增；82 项通过 | 非业务 PASS |
| R15-03 | 改动可能越出允许范围 | 只改现有五文件，`CLAUDE.md` 未动 | 相对 `e0de71b` 恰 5 个修改；相对 `5cb6817` 恰 6 个新增 | 未合并主树 |
| R15-04 | 文档可能仍夸大深度行为 | README/AGENTS 明确 `depth+1` 入集合前立即拒绝、同级零消费；源发现 `os.walk` 仍是残差 | 文档与代码/测试同步 | 真实 ACL 未测 |
| R15-05 | 测试可能改写源码 | 运行前后计算六文件 SHA256 | 前后全部一致 | 不代表真实数据 |

本轮复核：最终交付限定在六文件范围内，归档可重跑，原 81 项保留；仍需原独立审核端复审新固定 SHA。

### 8.4 本轮三轮自审总结

- 实际命令：`python -m unittest discover -s tests/private_regression -p "test_*.py"`；82 项通过、0 失败、0 错误、0 跳过、退出 0；最终归档重跑同结果。
- 深度独立探针：before `yielded=112`、after `yielded=12`；当前目录 100 兄弟零消费、更深 `scandir` 零、全部 closed。
- 仍未验证：真实旧 XML/日志/provenance、真实 R3、真实 Windows ACL/并发替换、设备/ADB、网络/模型、独立标注与业务 PASS；源发现 `_walk_xml` 的 `os.walk` 逐目录模式仍是已披露未改残差。
- 结论仅为作者在合成范围内完成深度发现即停返修，必须由原独立审核端复审，不能以 82 项绿灯或本节自审代替。

## 9 39ebb28 Git 查询故障安全返修后三轮作者自审（合成范围）

> 本节针对主协调对 `39ebb28` 的 changes_requested 追加。仍是同一作者自审，不是独立审核、协调端验收或真实回归业务 PASS；只使用临时合成目录与临时 Git 仓库，未读取真实旧 XML/日志/provenance、旧 R3 或真实项目索引。

### 9.0 复审问题矩阵

| 编号 | 复审问题 | 整改落点 | 复验（合成） | 未测边界 |
|---|---|---|---|---|
| 阻塞 Git | `check-ignore`/`ls-files` 的 `returncode == 0` 吞掉运行故障；真实坏索引 128 被当作未跟踪 | 新增 `_git_query_returncode`，只接受 0/1；2/128/负码/OSError 固定 `git_unavailable`；两条查询共用 | 原独立探针 before：CLI0/ok/ignored=true/not_tracked=true/128×2 → after：CLI2/error/git_unavailable/无 safety/128×1 | 真实 ACL/索引修复未测 |
| 阶段隔离 | 初检/结束/validate 需要不同失败后果 | 初检在建输出前拒绝；结束错误回执不写成功 receipt；validate invalid | 三个真实坏索引测试 + 正控通过 | 真实 OS 并发未测 |
| 返回码矩阵 | 缺 0/1/2/128/负码与 OSError 显式覆盖 | 对两条查询做配对矩阵与启动失败 | 0/1 正常语义；2/128/-9/OSError 全部 git_unavailable | 仅合成 CompletedProcess 矩阵，真实 Git 另测 |
| 既有 82 项 | 修改 Git 逻辑可能回归 N2/状态/Unicode | 只改 Git 查询边界，保留全部旧断言 | 原 82 项全部保留，新增 5 项后 87 项通过 | 非独立批准 |

### 9.1 第十六轮：Git 返回码、阶段安全与原始证据

审查视角：是否区分正常匹配/非匹配与操作失败；初检/结束/validate 是否分别安全拒绝；是否使用真实临时索引。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R16-01 | 两条 Git 查询 `==0` 把 128 转成 false | 抽 `_git_query_returncode`，非 0/1 一律 `git_unavailable` | 真实坏索引 ls-files=128；同探针 after CLI2/status error/无 safety | 不修复 Git/索引 |
| R16-02 | 初检失败可能仍创建输出 | `_assert_output_safe` 在 mkdir 前调用，失败即抛 | 初检真实 128：输出根不存在、CLI2、固定错误回执 | 真实权限错误未测 |
| R16-03 | 结束失败可能仍返回成功回执 | 结束复核失败在写 receipt/manifest 前抛出；main 输出固定错误 | 结束真实 128：CLI2、无 success safety、无 safe_receipt/manifest，已写 XML 保留 | 不声称回滚 |
| R16-04 | validate 可能把查询失败当 valid | 保留流式/内容检查，Git 失败 `git_unavailable`/invalid | 独立 validate 与 CLI 均 invalid/退出 2 | 真实批次未跑 |
| R16-05 | 缺显式返回码矩阵/OSError | 配对测 check/ ls-files 的 0/1/2/128/-9 与 OSError | 0/1 正常；其余全部固定拒绝；正控仍 valid | CompletedProcess 模拟仅矩阵补充 |

本轮复核：Git 查询失败不再产生任何安全通过布尔；真实临时索引测试与返回码矩阵分别覆盖实际与显式边界。

### 9.2 第十七轮：原 82 项、N2 与状态/Unicode/日志保持

审查视角：Git 边界改动是否破坏原 82 项、流式消费/深度即停与既有状态语义。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R17-01 | 原 82 项可能被 Git 改动影响 | 只改 `_git_ignore_status`/validate 错误映射 | 原 82 项全部通过，新增 5 项后 87 项 | 非独立批准 |
| R17-02 | N2 文件/目录/深度消费可能回归 | 不改枚举实现 | 文件 305→257、目录 105→17、小阈值 5、深度 before112→after12、首项1/兄弟0/更深0/close | 真实大目录未跑 |
| R17-03 | log-only/状态矩阵可能回归 | 不改状态原因集合 | 16 例矩阵自洽、log-only 双入口 0/0 | 真实组合未跑 |
| R17-04 | Unicode/日志变化/链接可能回归 | 不改解析/时序/链接路径 | Unicode 解析 0 次、日志变化 error、合法/链接测试通过 | 真实 ACL 未测 |

本轮复核：返修限定在 Git 查询边界，原 82 项、N2 与已闭合状态语义保持。

### 9.3 第十八轮：最终归档、范围、哈希与敏感边界

审查视角：最终 Git archive 自包含、只改允许文件、原 82 保留、源码前后哈希、旧敏感提交仅 ancestry。

| 问题 | 实际发现 | 修订 | 复验证据 | 未测边界 |
|---|---|---|---|---|
| R18-01 | 工作区通过不等于归档自包含 | 从最终 `git archive` 解包运行同一 unittest 与关键探针 | 归档 87 项通过、Git 失败探针 after 一致、无未跟踪依赖 | 未跑真实源 |
| R18-02 | 新增测试可能删除旧判据 | 原 82 项全部保留，仅尾部新增 5 项 | 相对 `39ebb28` 测试文件纯新增；87 项通过 | 非业务 PASS |
| R18-03 | 改动可能越出允许范围 | 只改现有五文件，`CLAUDE.md` 未动 | 相对 `39ebb28` 恰 5 个修改；相对 `5cb6817` 恰 6 个新增 | 未合并主树 |
| R18-04 | 测试可能改写源码 | 运行前后计算六文件 SHA256；归档 blob 对比 HEAD | 前后一致、归档 blob 全部 MATCH | 不代表真实数据 |
| R18-05 | 旧敏感提交/隐私边界 | 仅 `merge-base --is-ancestor` 检查 `70f01e4`，不读 blob；不读真实数据/配置 | 非祖先退出 1；静态扫描仅 `@AGENTS.md` | 真实隐私未涉及 |

本轮复核：最终交付限定六文件范围，归档可重跑，原 82 项保留；仍需原独立审核端复审新固定 SHA。

### 9.4 本轮三轮自审总结

- 实际命令：`python -m unittest discover -s tests/private_regression -p "test_*.py"`；87 项通过、0 失败、0 错误、0 跳过、退出 0；最终归档重跑同结果。
- Git 独立探针：before fault CLI0/ok/ignored+not_tracked=true/128×2；after CLI2/error/git_unavailable/无 safety，正控仍 CLI0/ok。
- 仍未验证：真实旧 XML/日志/provenance、真实 R3、真实 Windows ACL/并发替换、真实项目索引、设备/ADB、网络/模型、独立标注与业务 PASS；源发现 `_walk_xml` 的 `os.walk` 逐目录模式仍是已披露残差。
- 结论仅为作者在合成范围内完成 Git 查询故障安全返修，必须由原独立审核端复审，不能以 87 项绿灯或本节自审代替。
