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

状态：已完成一次经协调端阶段审查放行的只读抽取。公开文档不记录真实执行日期、源路径、源文件名、哈希、唯一批次 ID、真实样本与扫描规模、业务事件计数或类别分布；以下只用固定占位符与定性结论，精确真实统计仅保留在本机 Git 忽略目录的安全回执中。

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
| 本机验证器 | `valid`，20 项检查全为 true |

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

## 4 本轮实际校验

```powershell
python -m unittest discover -s tests/private_regression -p "test_*.py"
```

最近一次结果：45 项测试通过、0 失败、0 错误，退出码 0。该结果只证明合成判据与工具链路。

P2 实际只读抽取与验证：抽取退出码 0，本机验证器退出码 0、`status=valid`，安全回执为 `partial`（原因见 §3.1）。真实安全回执只保存在本机忽略目录；公开提交只含 6 个工具/测试/指南文件。上述结果不代表真实模型、设备、独立标注或业务 PASS。
