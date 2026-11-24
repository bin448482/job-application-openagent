# fixtures/m2/probe 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `baseline.json`：合成输入文件。
- `inspect.json`：合成输入文件。
- `parse_exactly_five.json`：合成输入文件。
- `parse_future.json`：合成输入文件。
- `parse_over_five.json`：合成输入文件。
- `parse_stale.json`：合成输入文件。
- `seed_backwards.json`：合成输入文件。
- `seed_blocked.json`：合成输入文件。
- `seed_queued.json`：合成输入文件。
- `seed_queued_extra.json`：合成输入文件。
- `seed_running.json`：合成输入文件。
- `seed_terminal.json`：合成输入文件。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
