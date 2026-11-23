# framework 目录指南

## 一、目录用途

提供标准库实现的资产校验、严格比较、虚拟时钟/事件重放与验收分流。它只判断测试资产和真实产品输出，不实现未读聚合、调度、动作执行或任何业务替代逻辑。

## 二、文件清单

- `__init__.py`：包标识。
- `model.py`：加载 catalogue/oracles，校验 73 场景、变体引用、设备门槛、夹具结构与路径边界；错误不带绝对路径。
- `compare.py`：JSON 类型严格比较（bool≠整数）、路径/键集合/长度断言、普通业务 stdout 必须是单个 JSON 对象。
- `replay.py`：虚拟时钟与固定事件序列重放，校验起点/单调/非 bool 非负，保留 arrival/first_observed/handling_started 为独立字段。
- `m2_probe.py`：M2 受限探针，调用真实 `load_and_parse_snapshot`/`persist_and_aggregate`；仅用于 now 注入、合成初态 seed、只读 baseline/inspect，SQL 不替代生产转换；inspect 返回发现/租约/基线/消息/设备只读事实，baseline 记录完整五表+事件签名与“基线/消息/设备”稳定签名；request/db/fixture 先查原始 leaf/祖先链接，再 resolve/containment。
- `m1_probe.py`：M1 纯合成 JSON 脚本探针；只调用真实 `initialize_database`/`SqliteStore`，支持假时钟、固定 host/owner、停止检查器、显式 ProcessLog、受限 prepare/inspect 与 barrier/hold_lock；request/db/log/barrier 路径在读写开库前校验为隔离根内相对路径，拒绝绝对/..、自身或祖先链接；不接受任意 SQL/命令/路径。
- `m0_protocol_probe.py`：在隔离根仅替换 `subprocess.run` 设备边界，调用已交付 `AdbDeviceProbe` 验证超时/非零/不重试；不启动真实 ADB。复制该入口前复用父路径 symlink/junction 与 target 边界校验，不只检查文件存在。
- `runner.py`：`assets`/`acceptance` 分流，含 `IMPLEMENTED_MILESTONES` 门禁；acceptance 先校验资产，再在隔离根复制真实 app 与命令/manifest 选定的合成输入（排除 expected/oracle），使用最小环境白名单和 tripwire 拒绝网络/子进程，缺 app 记 BLOCKED。
- `AGENTS.md`：本文件。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、入口与行为

```powershell
python -m tests.scenarios.framework.runner --mode assets
python -m tests.scenarios.framework.runner --mode acceptance --target .
```

- assets 只看测试资产，退出 0 不是产品 PASS。
- acceptance 缺 `app/__main__.py`、里程碑未实现、入口未冻结、DEFERRED 或 MODULE_EVIDENCE 退 3；任一执行差异退 1；只有全部 F 变体执行通过才退 0，且报告仍标记设备门槛未验收。当前门禁为 M0/M1/M2；新增里程碑须先更新常量与指南。并发组只由父 runner 启动两个隔离子进程，全部 ready 才 go，spawn 失败/提前退出/超时均 FAIL，并在 try/finally 有界终止/回收全部已创建子进程；timeout 必须为 1–120 秒非 bool 整数，非法时零启动；子进程 tripwire 仍禁设备/网络/孙进程。
- 报告只写 `data/evals/scenario-runs/<run>/report.json`；隔离根同在其下，含 app 副本、夹具副本、tripwire 日志与临时库。

## 四、维护要求

- 不在此实现生产业务函数，也不 mock 被测业务函数；只替换设备/时钟/provider 等边界。
- 修改比较器或隔离逻辑必须补正反例：bool 充整数、非法 JSON、时钟回拨、路径越界、同名前缀目录、symlink/junction、凭据继承、网络/子进程 tripwire。
- 不输出环境变量值、原始异常链或本机绝对路径；新增错误只保留相对标识与异常类别。
