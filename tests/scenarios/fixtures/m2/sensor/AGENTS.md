# fixtures/m2/sensor 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `all_not_applicable/`：子目录，见其自身指南。
- `ambiguous_identity_plus_target/`：子目录，见其自身指南。
- `badge_ambiguous/`：子目录，见其自身指南。
- `badge_invalid_bounds/`：子目录，见其自身指南。
- `badge_outside/`：子目录，见其自身指南。
- `cross_badge_only/`：子目录，见其自身指南。
- `cross_badge_preview/`：子目录，见其自身指南。
- `duplicate_identity_only/`：子目录，见其自身指南。
- `empty_list/`：子目录，见其自身指南。
- `full_all_absent/`：子目录，见其自身指南。
- `future_snapshot/`：子目录，见其自身指南。
- `global_count_no_target/`：子目录，见其自身指南。
- `global_zero_preview_no_baseline/`：子目录，见其自身指南。
- `global_zero_unverified/`：子目录，见其自身指南。
- `global_zero_verified/`：子目录，见其自身指南。
- `global_zero_verified_but_positive/`：子目录，见其自身指南。
- `invalid_xml/`：子目录，见其自身指南。
- `missing_bottom_proof/`：子目录，见其自身指南。
- `missing_identity_field/`：子目录，见其自身指南。
- `multi_list/`：子目录，见其自身指南。
- `new_fingerprint/`：子目录，见其自身指南。
- `new_greeting_positive_unverified/`：子目录，见其自身指南。
- `new_greeting_zero_unverified/`：子目录，见其自身指南。
- `nine_plus/`：子目录，见其自身指南。
- `no_badge_same/`：子目录，见其自身指南。
- `page_corrupt/`：子目录，见其自身指南。
- `page_tab_conflict/`：子目录，见其自身指南。
- `path_traversal/`：子目录，见其自身指南。
- `positive/`：子目录，见其自身指南。
- `positive_other/`：子目录，见其自身指南。
- `preview_change_with_baseline/`：子目录，见其自身指南。
- `preview_no_baseline/`：子目录，见其自身指南。
- `region_null/`：子目录，见其自身指南。
- `row_unread_plus_preview/`：子目录，见其自身指南。
- `same_preview_missing_time/`：子目录，见其自身指南。
- `same_preview_new_time/`：子目录，见其自身指南。
- `same_preview_same_time/`：子目录，见其自身指南。
- `stale_snapshot/`：子目录，见其自身指南。
- `two_pages_overlap/`：子目录，见其自身指南。
- `uncalibrated_profile/`：子目录，见其自身指南。
- `version_mismatch/`：子目录，见其自身指南。
- `viewport_all_zero/`：子目录，见其自身指南。
- `wrong_page/`：子目录，见其自身指南。
- `zero_plus/`：子目录，见其自身指南。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
