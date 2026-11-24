# tests/fixtures 目录指南

## 一、目录用途

存放纯合成测试输入。所有值均为人工构造，不使用真实设备、账号、聊天、联系人、公司、序列号或个人事实。

## 二、当前子目录

- `doctor/`：M0 探针合成夹具；见 `doctor/AGENTS.md`。
- `sensor/`：M2 合成 profile、列表 XML、manifest 与独立 expected；见 `sensor/AGENTS.md`。
- `read/`：M3 合成页面/职位/未读/会话 profile、XML、manifest 与独立 expected；见 `read/AGENTS.md`。
- `policy-preview/`：M4a 固定 `policy-greeting` 等独立合成 preview 输入，生产 preview 从项目根读取；见 `policy-preview/AGENTS.md`。
- `bridge/`：M4b 合成本机桥桩，仅测试参数数组/角色分区/错误诊断；见 `bridge/AGENTS.md`。
- `actions/`：M5 纯合成动作观察脚本，只描述合成 IME 与一次观察/失败，不是 Evidence 或真实设备记录；见 `actions/AGENTS.md`。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、约束

- 夹具路径相对 manifest/CLI 参数解析，不使用绝对路径。
- 输入夹具不得引用答案或期望输出；期望由测试代码独立断言。`sensor/expected.json` 与 `read/expected.json` 仅测试读取，解析器不得引用。
- manifest 引用的 profile/XML/像素文件必须位于同一夹具目录内现有文件；拒绝绝对路径、`..`、符号链接/junction 与越界解析。
- 不写入敏感信息、真实业务数据或本机路径。
- M0 fixture 运行只创建自己的被忽略 `data/logs/m0-process-<运行 UUID>.jsonl`；不写业务库、不启动子进程、不连接设备或网络。M2 `sense` fixture 只读合成 manifest/profile/XML，可写项目内合成 SQLite，但仍零设备、零网络、零模型、零子进程；M3 `read` 只读合成 XML/RGBA，不写业务库，零设备/网络/模型/子进程。M4a `policy-preview` 只读固定合成案例，由 preview 纯渲染，零设备/网络/模型/子进程，且案例目录属于生产运行资源、不可假称可删除。M4b `bridge_stub.py` 只作为测试启动的合成本机进程，不联网、不调用真实模型、不读私有数据。M5 `actions/` 只提供合成观察给 `ScriptedActionExecutor`，零设备/网络/模型/子进程，不接收 Evidence 字段。
- 不预建后续里程碑的空目录。

## 四、维护要求

1. 新增/删除/改名夹具目录时同步更新本文件与根 `AGENTS.md`。
2. 新增夹具时说明用途、字段、期望结果来源与版本边界。
3. 夹具变更必须同步受影响测试；不得为通过实现而放宽独立期望。

## 五、已知限制

- 当前有 M0 doctor、M2 sensor、M3 read、M4a policy-preview、M4b bridge 与 M5 actions 夹具。
- M2/M3 profile 是合成开发契约，不代表任何真实 BOSS 版本已标定；M3 稳定消息标识、像素阈值与版式均未真机验证。M4a preview 案例只验证离线渲染与分区；M4b bridge 桩只验证桥协议与失败边界；M5 actions 只验证观察→三态/额度/对账，不证明真实 provider、模型能力、语义安全或发送授权。
- 独立场景协调端的夹具不在本 Task 范围。
