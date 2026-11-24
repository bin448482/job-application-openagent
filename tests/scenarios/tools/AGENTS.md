# tools 目录指南

## 一、目录用途

保存场景资产的作者生成脚本。脚本中的人工判据来自 P/A/K 契约与场景方案，机械展开为 catalogue、独立 expected 与合成夹具；不读取产品输出、真实配置、私人事实或历史业务库。

## 二、文件清单

- `m2_requests.py`：生成 M2 now 注入、seed 与 inspect 请求。
- `m1_requests.py`：生成 M1 纯合成 JSON 请求脚本（初始化/种子/租约/故障/竞争/inspect），供隔离探针读取；不含任意 SQL 文本。
- `build_assets.py`：定义 M0–M8 的 73 个场景、全部变体、预期断言与夹具生成逻辑；默认重建 `fixtures/` 后写出 `catalogue.json` 与 `expected/oracles.json`。M3 read 预期对 rows/cards/messages/field_evidence 的每个元素声明固定键与必要类型，并为非首项身份/摘要/证据写独立值；日志预期按单次 CLI 调用登记完整有序事件序列。M4 从公开 base/示例生成合成策略包与正负包、公开/留出套件和受限探针请求；M4c 生成事实负例/待办、共享门禁崩溃/篡改/换 profile/失败、真实租约桥前后、真实 restore/acceptance 的隔离请求；不把 expected 或真实私有样本写入夹具。
- `AGENTS.md`：本文件。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、使用

```powershell
python tests/scenarios/tools/build_assets.py
python -m tests.scenarios.framework.runner --mode assets
```

生成后必须运行资产自检与 unittest。脚本生成失败或自检失败时不得提交。

## 四、维护要求

- 判据先于夹具：新增场景/变体先在作者数据中写清独立预期，再生成输入。
- 不把产品输出、参考回放或模块单测结果写成 expected。
- 不引入第三方依赖；保持 Python 标准库。
- 修改脚本后同步本目录与上级 `tests/scenarios/AGENTS.md` 的计数和文件说明。
