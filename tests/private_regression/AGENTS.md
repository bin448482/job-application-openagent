# tests/private_regression 目录指南

## 一、目录职责

保存旧项目私有回归补充工具：受控发现 UI XML 与失败日志，按白名单生成纯合成派生样本、失败窗口、manifest、provenance 与安全回执；并提供本机校验器与纯合成自测。它只读源项目、不执行旧代码、不连接设备、不调用网络/模型，不替代合成黄金集或证明业务 PASS。全局工作规则见[根目录 AGENTS.md](../../AGENTS.md)。

本目录是独立任务边界；`tests/` 的共享索引条目由协调端/业务方串行维护，本目录不修改 `tests/AGENTS.md` 或根指南。

## 二、当前结构

- `extract.py`：标准库工具。受控发现、路径/链接/重叠/覆盖防护、XML 白名单重构与异常合成代表、日志固定类别时序与失败窗口、安全回执、provenance、本机校验器。
- `test_private_regression.py`：45 项 `unittest` 纯合成自测，覆盖拓扑递归、属性/文本白名单、DTD/实体/编码/超限/深度/节点异常、路径逃逸、junction 祖先与子目录、输出重叠/覆盖/未忽略、源不变、去重/抽样、provenance 对齐、时序感知去重、长簇分片与尾部类别、物理行上限、NaN/Infinity、固定时序类别、缺时钟/乱序/截断、旧 oracle 不可信、stdout/异常泄露、只含 `needs_label`、XML 字符正文、未知自由字段、验证器拒绝坏产物。
- `README.md`：公开使用说明、预算、输出 schema、固定类别与限制。
- `SELF_REVIEW.md`：R1 数据边界/脱敏、R2 故障输入/抽样限制/独立 expected、R3 真实执行/隔离/报告/待提交内容三轮自审记录；R3 只记录一次放行的实际只读抽取、验证器、脱敏后的安全回执结论与 `partial` 局限，不含真实样本/扫描规模、业务计数分布、字节数、批次 ID 或本机路径，精确统计仅留在本机 Git 忽略目录的安全回执。
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
- 发现/读取/校验全程有界；命中上限在回执标记 `truncated`/`partial`，不静默继续。
- 源在抽取期间改变时工具以 `error`/退出码 2 结束，不能把安全条件失败当作成功。
- 不新增第三方依赖；Python >=3.13 标准库。

## 五、验证方式

`python -m unittest discover -s tests/private_regression -p "test_*.py"` 运行纯合成自测，不需要真实源、Git 远端、设备、网络或模型；部分 junction 用例在无法创建 junction 时跳过。实际抽取与 `--validate` 需协调端阶段放行；本轮 R3 已执行一次只读抽取与验证，结果与 `partial` 局限见 `SELF_REVIEW.md`。

## 六、已知限制

- UI 派生样本只保留结构/几何/白名单状态线索，不证明语义或控件含义。
- 统一文本标记丢失文案差异；结构去重可能合并仅文案不同的快照。
- 日志白名单覆盖有限，未识别事件如实计为 `unknown`，失败窗口覆盖可能为 0。
- 相对时间只在窗口内有效；缺失/乱序/截断只标记，不修复。
- 日志被预算截断时只做大小/时间完整性校验，并标记 `log_integrity_scope=prefix`。
- 不验证 SQLite 持久化、跨会话检索、设备执行或真实业务正确性。

## 七、同步维护要求

修改文件名、入口、回执字段、固定类别、预算、输出布局、命令或测试覆盖时，同步更新本指南、`README.md`、`SELF_REVIEW.md` 与受影响的根/父目录索引（共享索引由协调端串行维护）。新增/删除/移动文件时同一变更更新本索引。全局规则只在根指南维护。
