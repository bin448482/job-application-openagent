# app/adapters/simulated 目录指南

## 一、目录用途

提供 M5 离线验收使用的脚本化设备动作执行器。它只按显式合成脚本回放观察，不连接设备、不启动子进程、不调用模型或网络，也不接收调用方直接给出的 Evidence。

## 二、当前文件

- `__init__.py`：声明合成适配器包。
- `actions.py`：`ScriptedActionExecutor`；固定 `current_ime`、`observation`、`raise` 三种脚本键，严格校验观察字段集合与类型，`raise` 只携带固定失败原因与 `clicked` 事实；返回 `ActionObservation`，由领域纯函数推导三态证据。
- `runtime.py`：M6 `StimulusRuntimeScene` 回放 M3 manifest 引用、可选 `targeted_read_fixture` 定向引用、原始现场与合成 bridge 响应字段；`FixtureM3Read` 调用真实 M3 解析/门禁并累计 fixture 采集次数，`take_device_calls` 返回并清零供生产预算扣减；该次数只代表 fixture 采集，不冒充真实 ADB dump/cat/screencap 命令数；`SyntheticDecisionBridge` 只提供原始 transport 响应，由唯一 `decide` 校验；严格拒绝 `expected/oracle/verdict/answer`，不把 fixture 的 task outcome/read result/Decision 当权威。可选定向键不要求旧场景补齐。
- `actions.py` 追加 `CommandLedgerActionExecutor`：用真实 `CommandIdentityStore` 包裹合成观察，实际记录 begin→PID→complete，供 apply_job 的 perform_action E2E 使用，不启动 ADB。

## 三、边界

- 仅用于测试和 CLI 合成动作闭环；不得在生产运行中替代真实设备适配器。
- 不接受 `outcome/effect` 等 Evidence 字段；未知键、缺字段、坏 UUID/时间/计数均拒绝。
- 不写正文、坐标、身份或路径到日志；不读真实数据库、配置或设备。

## 四、测试

`python -m unittest tests.test_action_executor tests.test_action_cli tests.test_runtime_execute`

`tests.test_runtime_execute` 只使用临时项目/OS 根、合成场景与合成凭证，验证调度/安全点/预算/游标/双时钟/read_only/决策记忆；不连接设备或 provider。

## 五、维护要求

1. 修改脚本 schema 或字段时同步 `app/domain/actions.py`、`app/cli/AGENTS.md`、`tests/AGENTS.md`、根 `AGENTS.md` 与 `docs/operator.md`。
2. 每个函数/方法写 docstring；不引入第三方依赖。
3. 不把合成执行器表述为真实设备执行能力。
