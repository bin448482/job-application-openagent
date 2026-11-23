# framework 目录指南

## 一、目录用途

提供标准库实现的资产校验、严格比较、虚拟时钟/事件重放与验收分流。它只判断测试资产和真实产品输出，不实现未读聚合、调度、动作执行或任何业务替代逻辑。

## 二、文件清单

- `__init__.py`：包标识。
- `model.py`：加载 catalogue/oracles，校验 73 场景、变体引用、设备门槛、夹具结构与路径边界；错误不带绝对路径。
- `compare.py`：JSON 类型严格比较（bool≠整数）、路径/键集合/长度断言、普通业务 stdout 必须是单个 JSON 对象。
- `replay.py`：虚拟时钟与固定事件序列重放，校验起点/单调/非 bool 非负，保留 arrival/first_observed/handling_started 为独立字段。
- `runner.py`：`assets`/`acceptance` 分流；acceptance 先校验资产，再在隔离根复制真实 app 与命令/manifest 选定的合成输入（排除 expected/oracle），使用最小环境白名单和 tripwire 拒绝网络/子进程，缺 app 记 BLOCKED。
- `AGENTS.md`：本文件。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、入口与行为

```powershell
python -m tests.scenarios.framework.runner --mode assets
python -m tests.scenarios.framework.runner --mode acceptance --target .
```

- assets 只看测试资产，退出 0 不是产品 PASS。
- acceptance 缺 `app/__main__.py` 退 3；任一执行差异退 1；只有全部 F 变体执行通过才退 0，且报告仍标记设备门槛未验收。
- 报告只写 `data/evals/scenario-runs/<run>/report.json`；隔离根同在其下，含 app 副本、夹具副本、tripwire 日志与临时库。

## 四、维护要求

- 不在此实现生产业务函数，也不 mock 被测业务函数；只替换设备/时钟/provider 等边界。
- 修改比较器或隔离逻辑必须补正反例：bool 充整数、非法 JSON、时钟回拨、路径越界、同名前缀目录、symlink/junction、凭据继承、网络/子进程 tripwire。
- 不输出环境变量值、原始异常链或本机绝对路径；新增错误只保留相对标识与异常类别。
