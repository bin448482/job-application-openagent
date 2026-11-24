# tests/fixtures/policy-preview 目录指南

## 一、目录用途

存放 M4a `policy preview` 的独立纯合成输入。生产 preview 与测试都从项目根固定读取本目录，不读取 `evals/cases.json`、参考 provider、expected、私有回归或留出答案。该目录是公开运行资源，部署时必须随项目携带，不能当作可删除的临时测试产物。

## 二、当前文件

- `policy-greeting.json`：HR 问候类合成案例，固定包含于发布 ID 清单。
- `policy-job-card.json`：职位卡合成案例，验证同一 Decision schema 的职位合同与 system 选择。
- `policy-anomaly.json`：异常原因摘要合成案例，两个 subject 字段均为 null。
- `policy-injection.json`：含伪造角色/闭合 JSON 诱导的不可信原文，仅用于验证原文只进入 user JSON 数据区，不进入 system/developer。
- `AGENTS.md`、`CLAUDE.md`：本目录索引与入口。

## 三、案例 schema

每个文件恰好包含 `schema_version/case_id/now/task_id/nonce/kind/subject/inbound/context/allowed_facts`，所有键必填、拒绝额外键：

- `schema_version` 为整数 1；`case_id` 为小写字母开头的受限词法并与文件名一致。
- `now` 为毫秒 UTC；`nonce` 为规范 UUID；`task_id` 为非空字符串。
- `kind` 仅 `hr_message/job_card/anomaly`；HR 的 `conversation_key` 非空，职位的 `job_brief` 非空。
- `inbound` 为非空字符串；`context` 至多两条字符串；`allowed_facts` 每项固定 `key/value/source/expires_at`，`source` 仅 `user_confirmed`。
- 案例不包含期望输出、答案、断言、模型/provider 或真实业务内容。

## 四、约束

- 所有值均为合成；不写真实姓名、公司、聊天、联系方式、设备、凭据或本机路径。
- 案例只提供 data 投影输入，不预先计算 `input_hash/prompt_hash`，也不包含黄金答案；摘要由实际 base/包与程序计算。
- 修改案例时同步 `tests/test_policy_contract.py`、`tests/test_policy_pack.py`、`tests/test_policy_store.py`、`docs/operator.md` 与根 `AGENTS.md`；不得为通过实现放宽 schema。
