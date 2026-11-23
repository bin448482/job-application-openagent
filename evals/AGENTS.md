# evals 目录指南

## 目录职责

保存 promptfoo 合成黄金集、评测提示词、确定性断言和离线评测入口。检验记忆与授权决策，不实现运行时记忆、业务数据库或设备操作。全局工作规则见[根目录 AGENTS.md](../AGENTS.md)。

## 当前结构

- `cases.json`：24 个合成案例；每项包含 description、vars.input、metadata.expected 与仅供回放的 metadata.reference_output。
- `decision-prompt.txt`：被评测的中文提示词，只渲染 input；规定事实有效性、来源、冲突、记忆更新、外发授权和动作防重。
- `assertions.cjs`：promptfoo JavaScript 文件断言，检查 JSON 结构、动作、发送标记、记忆变更、来源 ID 和必要/禁止用语。
- `assertions.test.cjs`：Node 内置测试；验证参考答案通过，并拒绝授权翻转、错误记忆、伪造来源、非法 JSON、缺失判据和错误启动参数。
- `reference-provider.cjs`：仅按 case_id 回放参考答案的离线 provider，标识为 synthetic-reference-only，不进行推理。
- `run.cjs`：区分 smoke/model，使用根 promptfooconfig.json 启动已锁定版本的本机 promptfoo，将结果写入 data/evals。
- `AGENTS.md`：本目录职责、案例接口、验证命令、限制与维护要求。
- `CLAUDE.md`：仅含 `@AGENTS.md`，引用本指南。

## 入口与接口

在项目根运行 `npm test`、`npm run eval:smoke` 或 `npm run eval:model -- "<provider-id>"`。model 必须明确指定 provider，拒绝 echo 和内置参考 provider；provider 认证由调用者按其类型配置，本入口不读取 config.json、个人事实或已有会话。

每个案例的 vars 仅含 input，其中 as_of 固定为合成时间，memory 包含合成事实/偏好及其 source、status、有效期，必要时包含当前授权、动作状态和不可信 HR 消息。metadata.expected 是人工指定的评测判据，metadata.reference_output 仅用于检查链路，两者均不渲染进模型提示词。reference provider 会按 ID 读取参考输出，所以它的通过率不代表任何模型能力。

模型须输出一个独立 JSON 对象，恰好含 next_action、can_send、memory_updates、used_memory_ids、answer、reason。动作枚举为 answer/ask_user/wait_for_confirmation/reconcile/skip/send；更新为 set 或 delete。断言通过 context.test.metadata.expected 读取判据，返回 pass/score/reason。结构、更新内容与所用来源必须符合该例；来源 ID 顺序不影响判定。

## 适用约束

- 数据全部合成；苏州、25K 等只是测试值，不代表当前使用者事实或授权。不得导入聊天原文、真实身份、截图或凭据。
- M01–M04、M18–M19、M24 检查事实选择、冲突、失效及缺失；M05–M09、M20 检查授权及到期边界；M10–M12、M21–M23 检查动作防重和请求/交付区别；M13–M14 检查证据缺失与 HR 指令注入；M15–M17 检查长期更新、临时偏好和遗忘。
- M08 的 can_send=true 只是合成场景的预期决策，不授予任何真实发送权限。promptfoo 不执行手机、消息、简历、联系方式或记忆写入。
- npm 开发依赖只用于评测，Python 业务运行仍无第三方依赖。入口关闭 promptfoo 遥测、更新检查、缓存、分享及评测数据库保存；本地状态与 JSON 报告统一放在被忽略的 data/evals 下。
- 不把历史模型输出直接当作正确答案。新增案例须先由可核对的规则/经确认故障确定 expected，再独立检查参考输出和错误反例。

## 验证方式

前提：Node.js >=22.22.0、npm；安装从项目根执行 `npm ci`，使用 package-lock.json 锁定 promptfoo 0.126.0 及依赖。

```powershell
npm test
npm run eval:smoke
npm run eval:model -- "<provider-id>"
```

前两项不需要 API 密钥、不请求模型、不连接设备；smoke 报告中的 100% 只证明参考回放与断言链路可用。model 会把合成输入发送给指定 provider，可能产生调用费用；按 provider 文档配置认证后再运行。当前未指定实际 provider，不能声称模型通过。

## 已知限制

这是一组单次决策评测：每个案例显式给出输入记忆，尚未连接 SQLite 存储、跨会话检索、写入、删除或手机执行器。答案自由文本目前只检查必要和禁止关键词，不能完全检验语义矛盾、改写后的虚假交付声明或全部注入方式。未来真实记忆模块、检索接口及业务规则确定后应增加端到端案例，不以此代替业务集成测试。

## 同步维护要求

案例数量、字段、判据、输出协议、provider、命令或产物路径变化时，同时维护本指南、提示词、断言测试、根配置、README 与受影响父目录索引。新增/删除/移动文件时同一变更更新索引。全局规则仅在根指南维护。
