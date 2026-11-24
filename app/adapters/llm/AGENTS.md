# app/adapters/llm 目录指南

## 一、目录用途

实现 M4b `DecisionBridgePort` 的两个可运行子进程适配器：架构 A 外部 agent 与架构 B 本机进程。二者都只接收已渲染的固定 `system/developer/user` 三消息、独立协议回显对象、`tools=[]` 与 `history=[]`，不接收输入指定 role、工具调用或额外消息。导入本目录不启动子进程、不读取环境变量或凭据。

## 二、当前文件

- `__init__.py`：声明模型桥适配器包。
- `support.py`：共用 `SubprocessBridge`；校验显式 argv 数组、`model_id` 与固定协议，构造规范请求，使用 `shell=False`、`stderr=DEVNULL`、20 秒总预算与 `tempfile.TemporaryFile` 承载 stdin/stdout（本机系统临时目录、进程退出/close 后删除、不写入报告）；总预算覆盖临时文件创建、输入写、Popen 提交边界，准备阶段超预算以 `bridge_timeout`/submitted=false 在 Popen 前拒绝；运行中每 20ms 检查输出增长，超过 `64KiB` 立即 kill，结束后再查一次并只读取 `64KiB+1`；输出监测 seek/tell 或 wait 的 OSError 转 `bridge_protocol` 并 kill/close；严格拒绝重复 JSON 键、坏 UTF-8、坏协议、角色声明不符与超限输出；把启动失败、超时、认证、额度、断连与不可用映射为固定 `BridgeError`，不泄露原异常或 stderr。超时/超限 kill、wait、close 并回收直接子进程，不启动读取线程。
- `bridge_a.py`：`ExternalAgentBridge`，provider `external-agent`，协议 `m4b-agent-v1`，成功响应固定 `{protocol,roles,output}`。
- `bridge_b.py`：`LocalProcessBridge`，provider `local-process`，协议 `m4b-local-v1`，成功响应固定 `{protocol,roles,content}`。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、行为与信任边界

1. 请求固定为三消息并按 system/developer/user 顺序发送；base 为 system，已校验有效策略为 developer，不可信 envelope 投影为独立 user JSON 字符串；协议回显字段在顶层独立 `echo` 对象，不从 user 数据或输入创建新角色。
2. 参数数组直接传给 `subprocess.Popen`，不经过 shell；`argv` 中不得放凭据。适配器不自行重试，重试只在 `usecases/decision.py` 依据传输层可证明未启动的固定错误执行一次；重试前用例重读时钟/租约/包，并用唯一 render 校验器复核事实有效期。
3. 进程已启动后的任何失败（超时、坏 JSON、坏回显、坏角色声明、非零退出、stdout 超限、输出监测/等待 IO 错误）都视为可能已提交，不重试；超限/超时/IO 失败 kill、wait、close 并回收直接子进程，不残留读取线程。临时文件不阻塞 EOF；若子进程自行派生短命后代并继承句柄，适配器只保证自身有界返回与直接子进程回收，不推断所有后代已退出。`stderr` 丢弃，不写日志或异常。桥返回的 `roles/tools/history` 只是协议声明，不能证明任意外部 provider 的真实运行行为；公开材料不得把它写成第三方能力已验证。
4. 角色声明必须恰为 `system/developer/user`；不支持三角色分区、响应协议不符或角色合并时受控拒绝。禁止把三角色拼成单字符串冒充隔离。
5. 输出只返回 Decision JSON 文本，不执行命令、模板或工具；不读项目文件、agent 会话、提示词目录或评测答案。
6. 过程日志由 usecase 记录 `decision/bridge` 阶段固定结果与原因；适配器不写 argv、端点、凭据、正文或原异常。

## 四、测试

```powershell
python -m unittest tests.test_bridge tests.test_policy_eval
```

测试用 `tests/fixtures/bridge/bridge_stub.py` 作为无网络本机参数数组进程，覆盖 A/B 成功、固定三角色分区、伪造 role 保持为 user 数据、真实启动失败一次重试、认证/额度/断连/坏 JSON/重复键/角色不符/坏回显固定诊断、2MiB stdout 超限、慢进程/不读 stdin 超时、父进程退出但子进程持有 stdout 的有界收尾与大 stderr 丢弃；真实外部 provider、真实模型与本机留出未测。

## 五、维护要求

1. 修改协议、超时、角色或诊断时，同步更新 `app/ports/decision.py`、`app/usecases/decision.py`、根 `AGENTS.md`、操作手册与测试。
2. 新增 provider 时必须证明固定角色边界与有界子进程参数数组；不接受只拼单字符串或静默换模型。
3. 每个源文件顶部写用途，每个函数/方法写 docstring。
