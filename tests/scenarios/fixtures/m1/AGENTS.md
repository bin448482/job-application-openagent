# fixtures/m1 目录指南

## 一、目录用途

由 `tools/build_assets.py` 生成的纯合成夹具目录，只包含测试输入或声明式场景数据；不含真实业务数据。

## 二、直接子项

- `l01_init_midway.json`：合成输入文件。
- `l01_missing_index.json`：合成输入文件。
- `l01_nonempty_no_version.json`：合成输入文件。
- `l01_unknown_version.json`：合成输入文件。
- `l02_claim_new.json`：合成输入文件。
- `l02_claim_unread.json`：合成输入文件。
- `l02_claim_unread_2.json`：合成输入文件。
- `l02_device_row.json`：合成输入文件。
- `l02_setup_one.json`：合成输入文件。
- `l02_setup_two.json`：合成输入文件。
- `l02_ttl.json`：合成输入文件。
- `l02_verify.json`：合成输入文件。
- `l03_clock_changed.json`：合成输入文件。
- `l03_duplicate_finish.json`：合成输入文件。
- `l03_duplicate_start.json`：合成输入文件。
- `l03_expired.json`：合成输入文件。
- `l03_old_lease.json`：合成输入文件。
- `l03_old_nonce.json`：合成输入文件。
- `l03_renew.json`：合成输入文件。
- `l04_alive.json`：合成输入文件。
- `l04_confirm_false.json`：合成输入文件。
- `l04_confirm_string.json`：合成输入文件。
- `l04_different_host.json`：合成输入文件。
- `l04_leased_stopped.json`：合成输入文件。
- `l04_permission.json`：合成输入文件。
- `l04_running_stopped.json`：合成输入文件。
- `l04_unknown.json`：合成输入文件。
- `l05_device_update_miss.json`：合成输入文件。
- `l05_event_write_fail.json`：合成输入文件。
- `l05_evidence_unknown.json`：合成输入文件。
- `l05_rollback_claim_busy.json`：合成输入文件。
- `l05_task_update_miss.json`：合成输入文件。
- `l05_undetermined.json`：合成输入文件。
- `l06_change_owner.json`：合成输入文件。
- `l06_parent_child_unknown.json`：合成输入文件。
- `l06_ttl_expired.json`：合成输入文件。
- `l07_bool_integer.json`：合成输入文件。
- `l07_busy_inline.json`：合成输入文件。
- `l07_cross_app.json`：合成输入文件。
- `l07_io_error.json`：合成输入文件。

## 三、维护要求

- 不手工修改生成文件；修改作者脚本后重新生成。
- `expected.json` 是 app 侧预期，不复制进 SUT 输入树；独立 oracle 在 `expected/oracles.json`。
- 不写入真实联系人、公司、聊天、设备标识、凭据或本机绝对路径。
