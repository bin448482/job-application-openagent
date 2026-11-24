# fixtures/m4/probe 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `d01_mapping.json`：合成输入文件。
- `d02_array.json`：合成输入文件。
- `d02_bool_version.json`：合成输入文件。
- `d02_evidence_not_substring.json`：合成输入文件。
- `d02_expired_nonce.json`：合成输入文件。
- `d02_extra_key.json`：合成输入文件。
- `d02_fenced_json.json`：合成输入文件。
- `d02_long_reply.json`：合成输入文件。
- `d02_missing_key.json`：合成输入文件。
- `d02_valid.json`：合成输入文件。
- `d02_wrong_hash.json`：合成输入文件。
- `d02_wrong_nonce.json`：合成输入文件。
- `d02_wrong_task.json`：合成输入文件。
- `d03_input_too_large.json`：合成输入文件。
- `d03_post_submit_timeout.json`：合成输入文件。
- `d03_pre_submit_error.json`：合成输入文件。
- `d04_expired_fact.json`：合成输入文件。
- `d04_injection.json`：合成输入文件。
- `d04_missing_fact.json`：合成输入文件。
- `d04_tier.json`：合成输入文件。
- `d04_uncertain.json`：合成输入文件。
- `d04_unconfirmed_source.json`：合成输入文件。
- `d05_runtime_changed.json`：合成输入文件。
- `d05_symlink.json`：合成输入文件。
- `d07_bad_roles.json`：合成输入文件。
- `d07_context_over.json`：合成输入文件。
- `d07_inject_confirm.json`：合成输入文件。
- `d07_inject_json.json`：合成输入文件。
- `d07_injection.json`：合成输入文件。
- `d07_role.json`：合成输入文件。
- `d07_role_anomaly.json`：合成输入文件。
- `d07_role_external.json`：合成输入文件。
- `d07_role_external_anomaly.json`：合成输入文件。
- `d07_role_job.json`：合成输入文件。
- `d07_role_local_job.json`：合成输入文件。
- `d07_subject.json`：合成输入文件。
- `d07_subject_extra.json`：合成输入文件。
- `d08_envelope_size.json`：合成输入文件。
- `d08_hash.json`：合成输入文件。
- `d08_mutations.json`：合成输入文件。
- `d08_render_size.json`：合成输入文件。
- `d10_restore_active.json`：合成输入文件。
- `d10_restore_stopped.json`：合成输入文件。
- `d10_restore_unknown.json`：合成输入文件。
- `d11_bridge_auth.json`：合成输入文件。
- `d11_eval_missing.json`：合成输入文件。
- `d11_eval_public.json`：合成输入文件。
- `d11_eval_terms.json`：合成输入文件。
- `d11_holdout_after_failed.json`：合成输入文件。
- `d11_holdout_after_retire.json`：合成输入文件。
- `d11_holdout_audit.json`：合成输入文件。
- `d11_holdout_failed_access.json`：合成输入文件。
- `d11_holdout_first.json`：合成输入文件。
- `d11_holdout_pack_edit.json`：合成输入文件。
- `d11_holdout_repeat.json`：合成输入文件。
- `d11_provider_auth.json`：合成输入文件。
- `d11_provider_quota.json`：合成输入文件。
- `d11_provider_timeout.json`：合成输入文件。
- `d11_register.json`：合成输入文件。
- `d11_register_duplicate.json`：合成输入文件。
- `d11_register_write_fail.json`：合成输入文件。
- `d11_regression_check.json`：合成输入文件。
- `d11_retire.json`：合成输入文件。
- `d11_retire_admission_fail.json`：合成输入文件。
- `d11_retire_missing.json`：合成输入文件。
- `d11_retire_registry_fail.json`：合成输入文件。
- `d11_unknown_suite.json`：合成输入文件。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
