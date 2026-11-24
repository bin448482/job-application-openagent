# evals/policy-suite 目录指南

## 目录职责

保存 M4b `policy eval` 的公开合成套件与旧 24 例逐项责任映射。公开套件只含单例输入与断言侧属性，不含模型答案、参考输出、其它案例或私有留出数据；旧 `evals/cases.json` 的 npm 回归保持完全独立。

## 当前文件

- `public.json`：公开合成套件 v1；每个案例固定 `input`（与 M4a preview 同一十键形状）与 `assertions`（intent 词表、必需/禁止子意图、回复非空与包含值、escalate 允许值）。覆盖地点/薪资许可事实正控制、缺失薪资待办、HR 注入、职位问题与职位卡（`interested` 且无 `human_review` 的有用分类正控制）。
- `mapping.json`：原 M01–M24 逐行登记原 ID、派生 ID、输入投影、独立属性、责任端、未接入原因与后续验法；存在不等于任何一条已经通过。
- `AGENTS.md`：本目录职责、接口、边界与维护要求。
- `CLAUDE.md`：仅含 `@AGENTS.md`。

## 接口与边界

- runner 由 `app/usecases/policy_eval.py` 实现；每次只把 `input` 投影进 envelope，断言侧不进入桥请求。答案/参考回放/其它案例不得进入模型输入。
- 地点与薪资正控制要求当前包 `facts.allow_keys` 允许对应键；默认示例允许键为空时，事实会被事实过滤丢弃，相关正控制会失败或转待办，不静默放宽。
- 公开集只验证有限契约，通过不证明语义安全、授权、设备动作或真实模型能力。模拟 transport 永不生成验收凭证。留出按语义输入指纹占用历史样本，访问 intent 在调用前持久化，退役整批写实际 regression 准入；私有留出另需绑定当前 model_id/model_config_hash 的本机无上传合同。
- 私有回归/留出位置为 `evals/private/regression/` 与 `evals/private/holdout/`，由 `.gitignore` 排除；当前不存在，零样本记为未测。留出运行只允许 `local-process`，并要求维护者/独立标注者/版本/数量/冻结时间/候选关联与访问记录。
- 原始 24 例的输入、expected、reference output 与断言不得复制进本目录或被新模型用于回填 Decision。

## 验证方式

```powershell
python -m unittest tests.test_policy_eval
python -m app policy eval --help
```

测试校验 24 行映射完整、派生 ID 属于公开套件、旧 24 文件未改、模拟/真实子进程路径报告摘要、失败无凭证与留出生命周期。

## 已知限制

真实 provider、私有回归与私有留出均未运行；公开套件不是完整业务判据，也不替代 M4c 事实与 M5 授权/动作接口。M5 已交付生产动作/资格/对账接口，但本表未自动重放旧 24 案例；旧案例不得回填 M5 Evidence 或授权结论。
