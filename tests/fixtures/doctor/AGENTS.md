# tests/fixtures/doctor 目录指南

## 一、目录用途

存放 M0 `doctor` 的纯合成探针输入。当前唯一文件是验收正例。

## 二、当前文件

- `ready.json`：单台合成设备、已选择 `primary`、解锁、前台为 `boss`；精确内容为
  `{"devices":[{"alias":"primary","state":"device"}],"selected_alias":"primary","unlocked":true,"foreground_app":"boss"}`。
  该值是实施计划 M0 的固定合成夹具，不是真实设备信息。

## 三、期望结果

`python -m app doctor --fixture tests/fixtures/doctor/ready.json` 必须退出 `0`，正常日志可用时 stderr 为空，输出
`{"schema_version":1,"status":"ok","reason_code":null,"device_count":1,"selected_alias":"primary","unlocked":true,"foreground_app":"boss"}`，且不新增业务库。该命令只追加被忽略的 `data/logs/m0-process.jsonl`；日志写入失败时 stdout/退出码不变，stderr 仅出现固定 `m0_process_log_degraded` 标记。

## 四、维护要求

1. 修改 `ready.json` 必须同步 `tests/test_doctor.py`、根 `AGENTS.md` 与实施计划中的固定值说明；不得为了通过实现而改夹具。
2. 负例夹具由测试在项目内临时目录生成，避免把大量错误输入固化为公开文件；如需新增固定文件，先确认独立判据与脱敏。
3. 本目录只放合成数据，不放真实序列号、窗口、聊天或个人事实。

## 五、已知限制

- 正例只验证 M0 输出形状与判定，不证明真实设备可用。
- 当前没有真实设备夹具；真实探针只能另行取得确认后单独记录。
- 夹具拒绝重复 JSON 对象键与不可 UTF-8 编码的孤立代理字符；负例由测试临时生成。
