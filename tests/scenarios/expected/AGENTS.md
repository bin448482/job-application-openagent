# expected 目录指南

## 一、目录用途

保存每个场景变体的独立 expected，供比较器与人工审查读取。预期由作者依据 P/A/K 契约先写，再生成夹具，不从产品输出反推。

## 二、文件清单

- `m6-observations.json`：M6 S01/S03/S06/S08 的只读期望轨迹，明确 `expected_observation_only_not_sut_input`；不复制进 SUT，不作为刺激喂给产品。
- `oracles.json`：以 `SC-.../变体` 为键，逐项记录层、摘要、断言、可执行 acceptance、禁止结果、源契约与前置。T/O/W 变体的 acceptance 固定为 null，保留未验收。
- `AGENTS.md`：本文件。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、边界与维护

- 夹具、manifest 与目标 app 都不得读取本目录；`framework/model.py` 会扫描夹具引用并在自检中拒绝答案串扰。
- 修改判据时必须同步 `tools/build_assets.py` 的作者数据并重新生成；不得直接改本 JSON 造成双源漂移。
- 预期中的路径只用项目相对路径；不得写入真实联系人、公司、聊天、设备标识或本机绝对路径。
- 新增/删改变体时同步更新 `catalogue.json`、本索引与上级 `tests/scenarios/AGENTS.md`。
