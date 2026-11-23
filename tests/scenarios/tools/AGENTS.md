# tools 目录指南

## 一、目录用途

保存场景资产的作者生成脚本。脚本中的人工判据来自 P/A/K 契约与场景方案，机械展开为 catalogue、独立 expected 与合成夹具；不读取产品输出、真实配置、私人事实或历史业务库。

## 二、文件清单

- `m1_requests.py`：生成 M1 纯合成 JSON 请求脚本（初始化/种子/租约/故障/竞争/inspect），供隔离探针读取；不含任意 SQL 文本。
- `build_assets.py`：定义 M0–M8 的 73 个场景、全部变体、预期断言与夹具生成逻辑；默认重建 `fixtures/` 后写出 `catalogue.json` 与 `expected/oracles.json`。
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
