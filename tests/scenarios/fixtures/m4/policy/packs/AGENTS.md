# fixtures/m4/policy/packs 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `bool_integer/`：子目录，见其自身指南。
- `contact_conflict/`：子目录，见其自身指南。
- `disclosure_missing/`：子目录，见其自身指南。
- `encoding_nul/`：子目录，见其自身指南。
- `eval/`：子目录，见其自身指南。
- `extra_key/`：子目录，见其自身指南。
- `missing_key/`：子目录，见其自身指南。
- `path_in_style/`：子目录，见其自身指南。
- `size_tone/`：子目录，见其自身指南。
- `tier_gap/`：子目录，见其自身指南。
- `tier_overlap/`：子目录，见其自身指南。
- `unsafe_a/`：子目录，见其自身指南。
- `valid/`：子目录，见其自身指南。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
