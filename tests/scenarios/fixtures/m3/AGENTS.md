# fixtures/m3 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `authorization_matrix.json`：合成输入文件。
- `cross_level_conflict.json`：合成输入文件。
- `entry_consumption.json`：合成输入文件。
- `version_drift.json`：合成输入文件。
- `probe/`：子目录，见其自身指南。
- `read/`：子目录，见其自身指南。
- `targeted/`：子目录，见其自身指南。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
