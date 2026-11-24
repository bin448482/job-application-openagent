# fixtures/m3/read 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `ambiguous_unread/`：子目录，见其自身指南。
- `conversation_ambiguous/`：子目录，见其自身指南。
- `conversation_boundary_diff_id/`：子目录，见其自身指南。
- `conversation_contact_mismatch/`：子目录，见其自身指南。
- `conversation_direction_conflict/`：子目录，见其自身指南。
- `conversation_incomplete/`：子目录，见其自身指南。
- `conversation_known/`：子目录，见其自身指南。
- `conversation_late_duplicate/`：子目录，见其自身指南。
- `conversation_newer/`：子目录，见其自身指南。
- `conversation_no_stable/`：子目录，见其自身指南。
- `conversation_no_stable_same_screen/`：子目录，见其自身指南。
- `conversation_older/`：子目录，见其自身指南。
- `conversation_other/`：子目录，见其自身指南。
- `conversation_overlap/`：子目录，见其自身指南。
- `conversation_truncated/`：子目录，见其自身指南。
- `invalid_xml_with_pixel/`：子目录，见其自身指南。
- `job_detail_complete/`：子目录，见其自身指南。
- `job_list_ambiguous/`：子目录，见其自身指南。
- `job_list_unique/`：子目录，见其自身指南。
- `job_missing_required/`：子目录，见其自身指南。
- `job_truncated/`：子目录，见其自身指南。
- `page_main/`：子目录，见其自身指南。
- `page_popup/`：子目录，见其自身指南。
- `page_unknown/`：子目录，见其自身指南。
- `pixel_absent_unverified/`：子目录，见其自身指南。
- `pixel_corrupt/`：子目录，见其自身指南。
- `pixel_present/`：子目录，见其自身指南。
- `pixel_resolution_mismatch/`：子目录，见其自身指南。
- `pixel_threshold_below/`：子目录，见其自身指南。
- `pixel_threshold_exact/`：子目录，见其自身指南。
- `unique_unread/`：子目录，见其自身指南。
- `version_drift/`：子目录，见其自身指南。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
