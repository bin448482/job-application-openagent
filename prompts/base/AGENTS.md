# prompts/base 目录指南

## 一、目录用途

存放 M4a 两份公开提示词基线，参与 `base_hash`、`prompt_hash` 与固定 system 投影。内容只写通用规则、Decision 契约与职位维度，不含个人策略、事实或真实业务片段。

## 二、当前文件

- `intent-and-reply.md`：HR 消息与异常场景的意图词表、Decision 输出契约、证据/披露/事实/档位安全下限。
- `job-evaluate.md`：职位评估维度与映射，复用同一个 Decision schema；不要求第二套输出。
- `AGENTS.md`、`CLAUDE.md`：本目录索引与入口。

## 三、契约

- 渲染时 HR/anomaly 选择 `intent-and-reply.md`，职位选择 `job-evaluate.md`；两份原文都进入 `base_hash` 与 `prompt_hash` 的 `base.files`，但单次 system 只选当前 kind 的合同。
- base 的摘要键固定为 `prompts/base/<文件名>` 的项目相对 POSIX 路径；换行、注释或任意字节变化都会改变摘要。
- base 不包含联系方式、个人事实、真实聊天、评测输入或答案；模型只能产出候选，程序仍校验 schema、回显、证据、披露、字数、禁止项与档位。
- 不支持的桥必须拒绝，不能把 system/developer/user 压成一段文本。

## 四、维护要求

1. 修改任一份 base 时同步更新 `BASE_VERSION`、`docs/operator.md`、README 与本目录；不得只改一份而声称合同未变。
2. 保持两份文件只要求同一个 Decision schema；新增字段必须先改架构 §5.2 与测试，不能在这里私设第二套输出。
3. 运行 validate/preview/契约测试后才能提交。
