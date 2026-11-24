# app/adapters/simulated 目录指南

## 一、目录用途

提供 M5 离线验收使用的脚本化设备动作执行器。它只按显式合成脚本回放观察，不连接设备、不启动子进程、不调用模型或网络，也不接收调用方直接给出的 Evidence。

## 二、当前文件

- `__init__.py`：声明合成适配器包。
- `actions.py`：`ScriptedActionExecutor`；固定 `current_ime`、`observation`、`raise` 三种脚本键，严格校验观察字段集合与类型，`raise` 只携带固定失败原因与 `clicked` 事实；返回 `ActionObservation`，由领域纯函数推导三态证据。
- `runtime.py`：M6 `StimulusRuntimeScene` 按顺序回放场景刺激（安全点、候选、读取状态、任务结果、决策刺激、时序/预算），严格拒绝 `expected/oracle/verdict/answer`；`StimulusDecisionSource` 只返回当前步骤的决策刺激，记录输入/事实摘要，不伪造真实模型调用或授权。

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
