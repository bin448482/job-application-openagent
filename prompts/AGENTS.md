# prompts 目录指南

## 一、目录用途

存放 M4a 公开提示词基线与纯合成策略包示例。`base/` 是两份公开 base；`examples/default/` 是可提交的四载荷示例，只作为 `policy init` 来源。`local/` 是本机活动包与 history 快照的运行时位置，默认被 Git 忽略，不在仓库中预建。

## 二、当前子目录

- `base/`：`intent-and-reply.md` 与 `job-evaluate.md` 两份公开 base；见 `base/AGENTS.md`。
- `examples/`：公开纯合成示例根；见 `examples/AGENTS.md`。
- `local/`：`policy init` 才创建的本机直接 profile 子目录与 `history/` 快照；不提交、不预建、不存放个人事实。
- `CLAUDE.md`：仅引用同目录 `AGENTS.md`。

## 三、契约

- base 的普通默认值可被合法策略字段替换，安全下限不能覆盖；两份 base 都要求同一个 Decision schema，职位场景只选 `job-evaluate.md` 的合同，不同时要求两套输出。
- `base_hash` 的输入固定为 `prompts/base/intent-and-reply.md` 与 `prompts/base/job-evaluate.md` 的项目相对 POSIX 路径到原文字节映射，不做换行或 Unicode 规范化。
- `local/` 只从 `prompts/local/<profile>` 读取一个活动包；`history/` 不是活动包，只能经 `policy restore` 恢复到新的 local 直接子目录。
- 四载荷文件、可选双指南、大小/路径/编码/敏感模式规则以公开仓库与策略包说明 §3 与 `docs/operator.md` 为准。公开目录不写入真实姓名、联系方式、聊天、公司关联、凭据或个人事实。
- preview 的生产入口固定读取 `tests/fixtures/policy-preview/` 的独立合成案例；该目录属于公开运行资源，部署时必须随项目携带，不能把测试目录当作可随意删除的临时产物。

## 四、维护要求

1. 修改 base 原文、示例四文件或 preview 固定路径时，同步更新 `docs/operator.md`、README、根 `AGENTS.md`、受影响目录指南与测试摘要正例。
2. 修改 base 必须视为发布版本变更：更新 `BASE_VERSION`、重算摘要并重跑 validate/preview；不得用同版本标签掩盖内容变化。
3. 不把黄金集输入、答案、metadata、断言或私有留出放入 base、示例、短语库或 few-shot。
