# fixtures/m3/probe 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `ladder_conflict_request.json`：合成输入文件。
- `ladder_context_request.json`：合成输入文件。
- `ladder_failed_request.json`：合成输入文件。
- `ladder_visual_request.json`：合成输入文件。
- `new_keys_cross_request.json`：合成输入文件。
- `new_keys_no_stable_request.json`：合成输入文件。
- `new_keys_request.json`：合成输入文件。
- `targeted_cross_profile_request.json`：合成输入文件。
- `targeted_exact_request.json`：合成输入文件。
- `targeted_fresh_request.json`：合成输入文件。
- `targeted_future_request.json`：合成输入文件。
- `targeted_global_positive_request.json`：合成输入文件。
- `targeted_older_request.json`：合成输入文件。
- `targeted_over_request.json`：合成输入文件。
- `targeted_same_request.json`：合成输入文件。
- `targeted_unknown_request.json`：合成输入文件。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
