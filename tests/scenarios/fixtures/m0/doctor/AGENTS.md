# fixtures/m0/doctor 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `ambiguous.json`：合成输入文件。
- `corrupt.txt`：合成输入文件。
- `foreground_other.json`：合成输入文件。
- `foreground_unknown.json`：合成输入文件。
- `invalid_duplicate_alias.json`：合成输入文件。
- `invalid_extra_key.json`：合成输入文件。
- `invalid_selected_missing.json`：合成输入文件。
- `locked_true.json`：合成输入文件。
- `locked_unknown.json`：合成输入文件。
- `no_device.json`：合成输入文件。
- `offline.json`：合成输入文件。
- `ready.json`：合成输入文件。
- `unauthorized.json`：合成输入文件。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
