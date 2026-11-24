# prompts/examples 目录指南

## 一、目录用途

存放可提交的纯合成策略示例，只作为 `policy init` 的复制来源。示例不包含个人事实、联系方式、真实聊天、评测答案或授权；示例通过不等于真实发送安全。

## 二、当前子目录

- `default/`：v1 四载荷示例（全 C、空事实白名单）；见 `default/AGENTS.md`。

## 三、契约

- `init` 只从 `prompts/examples/default/` 读取四载荷文件，把唯一 `profile_id` 行替换为目标目录名，并生成固定双指南。
- 示例目录不接受额外文件、子目录、链接或非 UTF-8 内容；示例的保守全 C 不等于所有意图的硬下限。
- 不把示例作为 few-shot、评测输入或真实话术来源。

## 四、维护要求

1. 修改示例四文件时同步更新 `tests/fixtures/policy-preview/` 的预期可用性、`docs/operator.md`、README 和测试。
2. 示例必须能通过 validate/preview；`allow_keys=[]` 时依赖事实的回复转待办。
3. 新增示例 profile 必须有独立用途，不能复制私有包或真实业务内容。
