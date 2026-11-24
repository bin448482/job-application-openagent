# prompts/examples/default 目录指南

## 一、目录用途

存放 `policy init` 使用的公开纯合成 v1 策略示例。四载荷文件均须有效、可公开提交，且不包含个人事实或真实业务数据。

## 二、当前文件

- `pack.toml`：`schema_version=1`、`profile_id="default"`、`zh-CN`、`apps=["boss"]`、披露“个人开发AI助手”、字数/冷却/每日上限、全 C 分档、空额外禁谈、固定三份 md 文件名、空事实白名单。
- `tone.md`：通用语气示例，不含个人值。
- `phrase-bank.md`：按 `## intent.sub_intent` 与 `- 短句` 语法组织的合成措辞。
- `never-say.md`：通用禁用字面表达示例。
- `AGENTS.md`、`CLAUDE.md`：本目录索引与入口。

## 三、契约

- 全 C 与空事实表示保守离线起点；用户只能按策略包说明 §3.2 的硬下限调整分档，A 档仍不因此获得发送授权。
- `init` 只替换 `profile_id` 行，不改写其它字节；示例自身必须通过 TOML/三 md 的全部 schema。
- 文件大小、编码、控制字符、联系方式与 Markdown 语法以策略包说明 §3 为准；不写评测输入、答案或真实话术。

## 四、维护要求

1. 修改任一文件时同步重算 `pack_hash`、更新受影响的 preview 正例与测试，并运行 `policy init/validate/preview`。
2. 不加入个人事实、真实公司/HR 话术、联系方式、凭据或私有留出内容。
3. 不把示例的“安全可跑”解释为设备、模型、发送或删除授权。
